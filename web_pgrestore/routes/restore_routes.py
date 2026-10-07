"""Restore start + SSE progress."""
from __future__ import annotations

import json
import time

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    request,
    stream_with_context,
)

from ..jobs import manager
from ..restore import is_valid_dbname
from ..security import rate_limited

restore_bp = Blueprint("restore", __name__)


@restore_bp.post("/restore")
@rate_limited(max_calls=5, period=60)
def start_restore():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    dbname = str(data.get("dbname") or "")
    source_db = str(data.get("source_db") or "")
    backup = str(data.get("backup") or "")
    backup_name = backup.rsplit("/", 1)[-1] if backup else ""

    if not is_valid_dbname(dbname):
        return jsonify({"error": "Invalid database name"}), 400
    if source_db and source_db != dbname and not is_valid_dbname(source_db):
        return jsonify({"error": "Invalid source database name"}), 400
    if not backup_name:
        return jsonify({"error": "Backup file required"}), 400

    cfg = current_app.config["APP_CFG"]
    job = manager.create(dbname, source_db or dbname, backup_name)
    job.ip = request.remote_addr or ""
    started = manager.try_start(job, cfg)
    if not started:
        return (
            jsonify(
                {
                    "error": "Too many concurrent restores",
                    "limit": cfg.MAX_CONCURRENT_RESTORES,
                }
            ),
            429,
        )

    return (
        jsonify(
            {
                "ok": True,
                "job_id": job.id,
                "progress_url": f"/restore/progress/{job.id}",
                "dbname": dbname,
                "backup": backup_name,
            }
        ),
        202,
    )


@restore_bp.get("/restore/progress/<job_id>")
def restore_progress(job_id: str):
    job = manager.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404

    @stream_with_context
    def event_stream():
        idx = 0
        idle_polls = 0
        while True:
            lines = job.snapshot_logs(idx)
            if lines:
                idle_polls = 0
                for line in lines:
                    idx += 1
                    yield (
                        "event: log\n"
                        + "data: "
                        + json.dumps({"line": line}, ensure_ascii=False)
                        + "\n\n"
                    )
            status = job.status
            if status in {"success", "failed"}:
                lines = job.snapshot_logs(idx)
                for line in lines:
                    idx += 1
                    yield (
                        "event: log\n"
                        + "data: "
                        + json.dumps({"line": line}, ensure_ascii=False)
                        + "\n\n"
                    )
                payload = dict(job.result or {})
                payload["ok"] = status == "success"
                payload["status"] = status
                payload["job_id"] = job.id
                yield (
                    "event: done\n"
                    + "data: "
                    + json.dumps(payload, ensure_ascii=False)
                    + "\n\n"
                )
                break
            idle_polls += 1
            if idle_polls % 8 == 0:
                yield ": ping\n\n"
            time.sleep(0.25)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@restore_bp.get("/restore/jobs/<job_id>")
def restore_job_status(job_id: str):
    job = manager.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(
        {
            "job_id": job.id,
            "status": job.status,
            "dbname": job.dbname,
            "backup": job.backup_name,
            "logs": job.snapshot_logs(),
            "result": job.result,
        }
    )

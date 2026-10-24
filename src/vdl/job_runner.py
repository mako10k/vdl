"""One isolated attempt. Only the supervising daemon changes job status."""
import json
import os
import sys
from pathlib import Path

from vdl.config import Config
from vdl.queue import JobQueue
from vdl.workers import run_job


def main():
    config = Config(home=Path(sys.argv[1]))
    job = JobQueue(config.db_path).get(int(sys.argv[2]))
    try:
        if job is None or job.status != 'running':
            raise RuntimeError('job is not running')
        result = {'ok': True, 'result': run_job(config, job)}
    except Exception as error:
        result = {'ok': False, 'error': str(error)}
    directory = Path(os.environ['VDL_ATTEMPT_DIR'])
    temporary = directory / 'result.tmp'
    temporary.write_text(json.dumps(result, ensure_ascii=False))
    temporary.replace(directory / 'result.json')


if __name__ == '__main__':
    main()

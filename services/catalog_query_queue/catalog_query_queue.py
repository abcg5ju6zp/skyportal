import time

from baselayer.app.env import load_env
from baselayer.app.models import init_db
from baselayer.log import make_log

from skyportal.models import DBSession
from skyportal.utils.catalog_query.jobs import queue_config, tick
from skyportal.utils.catalog_query.publish import publish_query

env, cfg = load_env()
log = make_log("catalog_query_queue")

init_db(**cfg["database"])


def service():
    while True:
        qcfg = queue_config()
        try:
            result = tick(
                session_factory=DBSession,
                publisher=publish_query,
                config=qcfg,
            )
            if not result["claimed"]:
                time.sleep(qcfg["poll_interval"])
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"Error during catalog query queue pass: {e}")
            time.sleep(min(qcfg["poll_interval"], 15))


if __name__ == "__main__":
    service()

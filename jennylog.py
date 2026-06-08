#! /usr/bin/python3

"""A logging function for Jenny"""

import os.path
import datetime
import filelock

from config import jennyconfig
from base import urlsep  # , logger


def log_action(
    user: str,
    ip: str,
    action: str,
    environments: list[str],
    distributions: list[str],
    packages: list[str],
) -> None:
    logpath = os.path.join(jennyconfig["jenny"]["logdir"], "jenny.log")
    timestamp = datetime.datetime.now()
    msgdata = {
        "timestamp": timestamp.isoformat(),
        "ip": ip,
        "user": user,
        "action": action,
        "environments": environments,
        "distributions": distributions,
        "packages": packages,
    }
    # For fun
    # import json
    # text = json.dumps(msgdata)
    # For practicality
    # logger.warning(msgdata)
    for i in ["environments", "distributions", "packages"]:
        msgdata[i] = urlsep.join(msgdata[i])
    text = "|".join(msgdata.values()) + "\n"
    lock = filelock.FileLock(logpath + ".lock", timeout=3)
    with lock:
        open(logpath, mode="a").write(text)

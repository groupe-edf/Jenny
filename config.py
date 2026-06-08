#! /usr/bin/python3

"""Parse and interpolate the Jenny configuration file into a hash"""

import os
import os.path
import sys
import logging
import yaml

from base import de2str

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

configfiles = ["/etc/jenny/config.yaml", os.path.expanduser("~/jenny/config.yaml")]

jennyconfig = {}


def load_config() -> None:
    raw = None
    for configfile in configfiles:
        try:
            with open(configfile, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            break
        except OSError:
            pass
        except yaml.YAMLError:
            pass
    if not raw:
        logger.error("Cannot find config file")
        sys.exit(1)

    jennyconfig["stages"] = raw["stages"]
    jennyconfig["jenny"] = raw["jenny"]
    jennyconfig["environments"] = raw["environments"]
    jennyconfig["dists"] = {}
    jennyconfig["distsperbasename"] = {}
    jennyconfig["proxy"] = raw["dists"]["_default"]["proxy"]
    jennyconfig["noproxy"] = raw["dists"]["_default"]["noproxy"]
    for d in raw["dists"]:
        if d == "_default":
            continue
        if "disabled" in raw["dists"][d] and raw["dists"][d]["disabled"]:
            continue

        for env in jennyconfig["environments"]:
            if env:
                dname = de2str(d, env)
            else:
                dname = d
            dr = raw["dists"]["_default"].copy()
            dr["env"] = env
            dr["basename"] = d
            for i in raw["dists"][d]:
                if i == "upstream":
                    continue
                dr[i] = raw["dists"][d][i]
            if "upstream" in raw["dists"][d] and env == jennyconfig["environments"][0]:
                # Add / end of url upstream to match with ArchiveRoot mirror get
                dr["upstream"] = (
                    raw["dists"][d]["upstream"]
                    if raw["dists"][d]["upstream"].endswith("/")
                    else raw["dists"][d]["upstream"] + "/"
                )
                dr["ismirror"] = True
            else:
                dr["ismirror"] = False
            dr["name"] = dname
            dr["architectures"] = dr["architectures"]
            dr["binary-architectures"] = [
                i for i in dr["architectures"] if i != "source"
            ]
            dr["has-sources"] = "source" in dr["architectures"]
            jennyconfig["dists"][dname] = dr
            if d not in jennyconfig["distsperbasename"]:
                jennyconfig["distsperbasename"][d] = {
                    "meta": {"stages": raw["dists"][d]["stages"]}
                }
            jennyconfig["distsperbasename"][d][env] = dr

    jennyconfig["publishes"] = {}
    for pub in raw["publishes"]:
        jennyconfig["publishes"][pub] = {}
        jennyconfig["publishes"][pub]["dists"] = []
        for d in raw["dists"]:
            if d == "_default":
                continue
            if "disabled" in raw["dists"][d] and raw["dists"][d]["disabled"]:
                continue
            if d not in raw["publishes"][pub]["dists"]:
                continue
            jennyconfig["publishes"][pub]["dists"].append(d)
        try:
            jennyconfig["publishes"][pub]["suffix"] = raw["publishes"][pub]["suffix"]
        except KeyError:
            jennyconfig["publishes"][pub]["suffix"] = ""
        try:
            jennyconfig["publishes"][pub]["env"] = raw["publishes"][pub]["env"]
        except KeyError:
            jennyconfig["publishes"][pub]["env"] = pub
        try:
            jennyconfig["publishes"][pub]["ignore-errors"] = raw["publishes"][pub][
                "ignore-errors"
            ]
        except KeyError:
            jennyconfig["publishes"][pub]["ignore-errors"] = False

        for i in ["type", "signexports", "signkey"]:
            jennyconfig["publishes"][pub][i] = raw["publishes"][pub][i]

        if jennyconfig["publishes"][pub]["type"] == "filesystem":
            jennyconfig["publishes"][pub]["path"] = raw["publishes"][pub]["path"]
            jennyconfig["publishes"][pub]["prefix"] = pub
        elif jennyconfig["publishes"][pub]["type"] == "s3":
            for v in ["endpoint", "bucket", "prefix", "keyid", "secretkey"]:
                jennyconfig["publishes"][pub][v] = raw["publishes"][pub][v]


load_config()

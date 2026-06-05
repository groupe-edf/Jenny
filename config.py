#! /usr/bin/python3

"""Parse and interpolate the Brian configuration file into a hash"""

import os
import os.path
import sys
import logging
import yaml

from base import de2str

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

configfiles = ["/etc/brian/config.yaml", os.path.expanduser("~/brian/config.yaml")]

brianconfig = {}


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

    brianconfig["stages"] = raw["stages"]
    brianconfig["brian"] = raw["brian"]
    brianconfig["environments"] = raw["environments"]
    brianconfig["dists"] = {}
    brianconfig["distsperbasename"] = {}
    brianconfig["proxy"] = raw["dists"]["_default"]["proxy"]
    brianconfig["noproxy"] = raw["dists"]["_default"]["noproxy"]
    for d in raw["dists"]:
        if d == "_default":
            continue
        if "disabled" in raw["dists"][d] and raw["dists"][d]["disabled"]:
            continue

        for env in brianconfig["environments"]:
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
            if "upstream" in raw["dists"][d] and env == brianconfig["environments"][0]:
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
            brianconfig["dists"][dname] = dr
            if d not in brianconfig["distsperbasename"]:
                brianconfig["distsperbasename"][d] = {
                    "meta": {"stages": raw["dists"][d]["stages"]}
                }
            brianconfig["distsperbasename"][d][env] = dr

    brianconfig["publishes"] = {}
    for pub in raw["publishes"]:
        brianconfig["publishes"][pub] = {}
        brianconfig["publishes"][pub]["dists"] = []
        for d in raw["dists"]:
            if d == "_default":
                continue
            if "disabled" in raw["dists"][d] and raw["dists"][d]["disabled"]:
                continue
            if d not in raw["publishes"][pub]["dists"]:
                continue
            brianconfig["publishes"][pub]["dists"].append(d)
        try:
            brianconfig["publishes"][pub]["suffix"] = raw["publishes"][pub]["suffix"]
        except KeyError:
            brianconfig["publishes"][pub]["suffix"] = ""
        try:
            brianconfig["publishes"][pub]["env"] = raw["publishes"][pub]["env"]
        except KeyError:
            brianconfig["publishes"][pub]["env"] = pub
        try:
            brianconfig["publishes"][pub]["ignore-errors"] = raw["publishes"][pub][
                "ignore-errors"
            ]
        except KeyError:
            brianconfig["publishes"][pub]["ignore-errors"] = False

        for i in ["type", "signexports", "signkey"]:
            brianconfig["publishes"][pub][i] = raw["publishes"][pub][i]

        if brianconfig["publishes"][pub]["type"] == "filesystem":
            brianconfig["publishes"][pub]["path"] = raw["publishes"][pub]["path"]
            brianconfig["publishes"][pub]["prefix"] = pub
        elif brianconfig["publishes"][pub]["type"] == "s3":
            for v in ["endpoint", "bucket", "prefix", "keyid", "secretkey"]:
                brianconfig["publishes"][pub][v] = raw["publishes"][pub][v]


load_config()

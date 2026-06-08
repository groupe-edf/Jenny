#! /usr/bin/python3

"""Aptly backend for Jenny

This module provides the primitives used by the rest of Jenny, backed
by Aptly.
"""

import re
import sys
import os
import os.path
import time
import json
import json.decoder
from typing import Callable, Any, Optional
import functools
import uuid
import requests
import inotify.adapters
import urllib.parse
import datetime
import dateutil.parser
import glob
from http.client import HTTPException
from debian import deb822, debfile
from zoneinfo import ZoneInfo
from base import PackageEntry, logger, de2str, dec2str, str2de, str2dec
from config import jennyconfig

tz = ZoneInfo("Europe/Paris")

jenny_basedir = jennyconfig["jenny"]["basedir"]
aptly_basedir = jennyconfig["jenny"]["aptly"]["basedir"]
aptly_apiport = jennyconfig["jenny"]["aptly"]["apiport"]
aptly_config_file = os.path.join(jenny_basedir, "aptly.conf")
batchsize = 1000

snapre = r"\w+"


def _fill_publish_prefixes():
    rootdirs = {}
    for bc_p in jennyconfig["publishes"]:
        if jennyconfig["publishes"][bc_p]["type"] != "filesystem":
            continue
        if "path" in jennyconfig["publishes"][bc_p]:
            rootdirs["fs0"] = jennyconfig["publishes"][bc_p]["path"]
            jennyconfig["publishes"][bc_p]["publishprefix"] = "fs0"
            break
    for bc_p in jennyconfig["publishes"]:
        if jennyconfig["publishes"][bc_p]["type"] != "filesystem":
            continue
        if "path" in jennyconfig["publishes"][bc_p]:
            bc_path = jennyconfig["publishes"][bc_p]["path"]
            if bc_path in rootdirs.values():
                continue
            newindex = "fs%d" % len(rootdirs)
            rootdirs[newindex] = bc_path
        else:
            bc_path = os.path.join(aptly_basedir, "public")
        irootdirs = {v: k for k, v in rootdirs.items()}
        index = irootdirs[bc_path]
        jennyconfig["publishes"][bc_p]["publishprefix"] = index
        logger.debug("Setting %s for %s", index, bc_p)
    if not rootdirs:
        default_rootdir = os.path.join(aptly_basedir, "public")
        rootdirs = {"fs0": default_rootdir}
        jennyconfig["publishes"][default_rootdir]["publishprefix"] = "fs0"
    jennyconfig["publishprefixes"] = rootdirs


_envvars = {
    "NO_PROXY": jennyconfig["noproxy"],
    "HTTP_PROXY": jennyconfig["proxy"],
    "HTTPS_PROXY": jennyconfig["proxy"],
    "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/games:/usr/games",
    "HOME": os.path.expanduser("~"),
}


class AptlyPackageEntry(PackageEntry):
    pass


class ApiException(Exception):
    pass


class AlreadyExistsException(Exception):
    pass


class InvalidNameException(Exception):
    pass


def _parseitem(s: str) -> dict:
    m = re.search(r"^P(\w+) ([-\w0-9.+~]+) ([^ ]*) ([^ ]*)", s)
    if not m:
        raise ValueError
    item = {
        "arch": m.group(1),
        "package": m.group(2),
        "version": m.group(3),
        "key": s,
        "akey": m.group(4),
    }
    return item


def _parsetope(s: str, dist: str, comp: str) -> AptlyPackageEntry:
    t = _parseitem(s)
    pe = AptlyPackageEntry(t["package"], t["version"], dist, comp, t["arch"])
    pe.key = t["key"]
    return pe


def _validate_snapshot_name(name):
    if not name or not re.search(r"^" + snapre + r"$", name):
        raise InvalidNameException("Not a valid snapshot name")


class AptlyApiClient:
    def __init__(self):
        self.api_get = functools.partial(self.api_request, requests.get)
        self.api_post = functools.partial(self.api_request, requests.post)
        self.api_put = functools.partial(self.api_request, requests.put)
        self.api_delete = functools.partial(self.api_request, requests.delete)
        t0 = time.time()
        while time.time() < t0 + 2:
            try:
                self.api_publish_list()
                break
            except requests.exceptions.ConnectionError:
                time.sleep(0.05)
                continue

    def api_request(
        self,
        call: Callable[..., requests.Response],
        sub_url: str,
        data: dict = None,
        params: dict = None,
        headers: dict = None,
        files: dict = None,
    ) -> tuple[int, Optional[Any]]:
        url = "http://localhost:%d/api/%s" % (aptly_apiport, sub_url)
        logger.debug(url)
        logger.debug(data)
        logger.debug(params)
        logger.debug(files)
        if data:
            if params:
                r = call(url, data=data, params=params, headers=headers, files=files)
            else:
                r = call(url, data=data, headers=headers, files=files)
        else:
            if params:
                r = call(url, params=params, headers=headers, files=files)
            else:
                r = call(url, headers=headers, files=files)

        try:
            return (r.status_code, r.json())
        except json.decoder.JSONDecodeError:
            return (r.status_code, None)

    def api_package_show(self, key: str) -> int:
        status_code, ret = self.api_get("packages/" + urllib.parse.quote(key, safe=""))
        logger.debug("api_package_show returned %d", status_code)
        return ret

    def api_mirror_list(self) -> Optional[Any]:
        status_code, ret = self.api_get("mirrors")
        logger.debug("api_mirror_list returned %d", status_code)
        return ret

    def api_mirror_delete(self, name: str, force: bool = False) -> int:
        status_code, _ = self.api_delete(
            "mirrors/" + urllib.parse.quote(name, safe=""),
            params={"force": 1 if force else 0},
        )
        return status_code

    def api_mirror_create(self, spec: dict) -> Optional[Any]:
        status_code, ret = self.api_post("mirrors", data=spec)
        logger.debug("api_mirror_create returned %d", status_code)
        return ret

    def api_mirror_update(self, name: str, spec=None) -> Optional[Any]:
        if spec is None:
            spec = {}
        status_code, ret = self.api_put(
            "mirrors/" + urllib.parse.quote(name, safe=""), data=spec
        )
        logger.debug("api_mirror_update returned %d", status_code)
        return ret

    def api_mirror_get(self, name: str) -> Optional[Any]:
        status_code, ret = self.api_get("mirrors/" + urllib.parse.quote(name, safe=""))
        logger.debug("api_mirror_get returned %d", status_code)
        return ret

    def api_mirror_packages(self, name: str, spec: dict) -> int:
        status_code, ret = self.api_get(
            "mirrors/" + urllib.parse.quote(name, safe="") + "/packages", params=spec
        )
        logger.debug("api_mirror_packages returned %d", status_code)
        return ret

    def api_mirror_snapshot(self, repo: str, snapshot_name: str) -> None:
        status_code, ret = self.api_post(
            "mirrors/" + urllib.parse.quote(repo, safe="") + "/snapshots",
            data={"Name": snapshot_name},
        )
        logger.debug("api_mirror_snapshot returned %d", status_code)
        return status_code, ret

    ## REPOS
    #############
    def api_repo_delete(self, name: str, force: bool = False) -> int:
        status_code, _ = self.api_delete(
            "repos/" + urllib.parse.quote(name, safe=""),
            params={"force": 1 if force else 0},
        )
        return status_code

    def api_repos_update(self, name: str, spec: dict) -> Optional[Any]:
        status_code, ret = self.api_put(
            "repos/" + urllib.parse.quote(name, safe=""), data=spec
        )
        logger.debug("api_repos_update returned %d", status_code)
        return ret

    def api_repos_get(self, name: str) -> Optional[Any]:
        status_code, ret = self.api_get("repos/" + urllib.parse.quote(name, safe=""))
        logger.debug("api_repos_get returned %d", status_code)
        return ret

    def api_repos_create(self, spec: dict) -> Optional[Any]:
        status_code, ret = self.api_post("repos", data=spec)
        logger.debug("api_repos_create returned %d", status_code)
        return ret

    def api_repos_list(self) -> Optional[Any]:
        status_code, ret = self.api_get("repos")
        logger.debug("api_repos_list returned %d", status_code)
        return ret

    def api_repos_add_packages(self, name: str, spec: list[str]) -> int:
        status_code, _ = self.api_post(
            "repos/" + urllib.parse.quote(name, safe="") + "/packages", data=spec
        )
        logger.debug("api_repos_add_packages returned %d", status_code)
        return status_code

    def api_repos_add_from_upload(self, name: str, dirname: str) -> int:
        status_code, _ = self.api_post(
            "repos/" + urllib.parse.quote(name, safe="") + "/file/" + dirname
        )
        logger.debug("api_repos_add_from_upload returned %d", status_code)
        return status_code

    def api_repos_delete_packages(self, name: str, spec: list[str]) -> int:
        status_code, _ = self.api_delete(
            "repos/" + urllib.parse.quote(name, safe="") + "/packages",
            data=json.dumps(spec),
            headers={"Content-Type": "application/json"},
        )
        logger.debug("api_repos_delete_packages returned %d", status_code)
        return status_code

    def api_snapshots_list(self):
        status_code, ret = self.api_get("snapshots")
        logger.debug("api_snapshots_list returned %d", status_code)
        return ret

    def api_snapshots_update(self, snapshot: str, spec: dict):
        data = json.dumps(spec)
        status_code, ret = self.api_put(
            "snapshots" + "/" + urllib.parse.quote(snapshot, safe=""), data=data
        )
        logger.debug("api_snapshots_update returned %d", status_code)
        return ret

    def api_snapshots_delete(self, snapshot: str):
        status_code, ret = self.api_delete(
            "snapshots" + "/" + urllib.parse.quote(snapshot, safe="")
        )
        logger.debug("api_snapshots_delete returned %d", status_code)
        return ret

    def api_snapshots_diff(self, left: str, right: str):
        status_code, ret = self.api_get(
            "snapshots"
            + "/"
            + urllib.parse.quote(left, safe="")
            + "/diff/"
            + urllib.parse.quote(right, safe="")
        )
        logger.debug("api_snapshots_diff returned %d", status_code)
        return ret

    def api_snapshots_packages(self, snap: str, spec=dict):
        status_code, ret = self.api_get(
            "snapshots/" + urllib.parse.quote(snap, safe="") + "/packages", params=spec
        )
        logger.debug("api_snapshots_packages returned %d", status_code)
        return ret

    def api_repos_snapshot(self, repo: str, snapshot_name: str) -> None:
        status_code, _ = self.api_post(
            "repos/" + urllib.parse.quote(repo, safe="") + "/snapshots",
            data={"Name": snapshot_name},
        )
        logger.debug("api_repos_snapshot returned %d", status_code)
        return status_code

    def api_repos_packages(self, repo: str, spec=dict):
        status_code, ret = self.api_get(
            "repos/" + urllib.parse.quote(repo, safe="") + "/packages", params=spec
        )
        logger.debug("api_repos_packages returned %d", status_code)
        return ret

    ## FILES
    #############

    def api_files_upload(self, dirname: str, files=dict):
        status_code, ret = self.api_post(
            "files/" + urllib.parse.quote(dirname, safe=""), files=files
        )
        logger.debug("api_files_upload returned %d", status_code)
        return ret

    def api_files_list_dirs(self):
        status_code, ret = self.api_get("files")
        logger.debug("api_files_list_dirs returned %d", status_code)
        return ret

    def api_files_delete_dir(self, dirname: str):
        status_code, ret = self.api_delete(
            "files/" + urllib.parse.quote(dirname, safe="")
        )
        logger.debug("api_files_delete_dir returned %d", status_code)
        return ret

    ## PUBLISH
    #############

    def api_publish_list(self) -> list:
        status_code, ret = self.api_get("publish")
        logger.debug("api_publish_list returned %d", status_code)
        return ret

    def api_publish_get(self, prefix: str, distribution: str) -> Optional[Any]:
        status_code, ret = self.api_get(
            "publish/"
            + urllib.parse.quote(prefix, safe="")
            + "/"
            + urllib.parse.quote(distribution, safe="")
        )
        logger.debug("api_publish_get returned %d", status_code)
        return ret if status_code == 200 else False

    def api_publish_create(self, prefix: str, spec=dict, asyncpub=False) -> int:
        status_code, ret = self.api_post(
            ("publish/" + urllib.parse.quote(prefix, safe="")),
            params={"_async": "true"} if asyncpub else {},
            data=json.dumps(spec),
            headers={"Content-Type": "application/json"},
        )
        logger.debug("api_publish_create returned %d/%s", status_code, ret)
        return status_code

    def api_publish_update(
        self, prefix: str, distribution: str, spec=dict, asyncpub=False
    ) -> int:
        status_code, ret = self.api_put(
            (
                "publish/"
                + urllib.parse.quote(prefix, safe="")
                + "/"
                + urllib.parse.quote(distribution, safe="")
            ),
            params={"_async": "true"} if asyncpub else {},
            data=json.dumps(spec),
            headers={"Content-Type": "application/json"},
        )
        logger.debug("api_publish_update returned %d/%s", status_code, ret)
        return status_code

    def api_publish_delete(
        self,
        prefix: str,
        distribution: str,
        force: bool = True,
        skipCleanup: bool = False,
    ) -> int:
        status_code, ret = self.api_delete(
            "publish/"
            + urllib.parse.quote(prefix, safe="")
            + "/"
            + urllib.parse.quote(distribution, safe=""),
            params={
                "force": 1 if force else 0,
                "skipCleanup": 1 if skipCleanup else 0,
            },
        )
        logger.debug("api_publish_delete returned %d/%s", status_code, ret)
        return status_code

    def api_publish_list_pending_changes(self, prefix: str, distribution: str) -> dict:
        status_code, ret = self.api_get(
            "publish/"
            + urllib.parse.quote(prefix, safe="")
            + "/"
            + urllib.parse.quote(distribution, safe="")
            + "/sources"
        )
        logger.debug("api_publish_list_pending_changes returned %d", status_code)
        return ret

    def api_publish_discard_pending_changes(
        self, prefix: str, distribution: str
    ) -> int:
        status_code, _ = self.api_delete(
            "publish/"
            + urllib.parse.quote(prefix, safe="")
            + "/"
            + urllib.parse.quote(distribution, safe="")
            + "/sources"
        )
        logger.debug("api_publish_discard_pending_changes returned %d", status_code)
        return status_code

    def api_publish_replace_source_components(
        self, prefix: str, distribution: str, spec=dict
    ) -> dict:
        status_code, ret = self.api_put(
            "publish/"
            + urllib.parse.quote(prefix, safe="")
            + "/"
            + urllib.parse.quote(distribution, safe="")
            + "/sources",
            data=json.dumps(spec),
            headers={"Content-Type": "application/json"},
        )
        logger.debug("api_publish_replace_source_components returned %d", status_code)
        return ret

    def api_publish_add_source_components(
        self, prefix: str, distribution: str, spec=dict
    ) -> dict:
        status_code, ret = self.api_post(
            "publish/"
            + urllib.parse.quote(prefix, safe="")
            + "/"
            + urllib.parse.quote(distribution, safe="")
            + "/sources",
            data=json.dumps(spec),
            headers={"Content-Type": "application/json"},
        )
        logger.debug("api_publish_add_source_components returned %d", status_code)
        return ret

    def api_publish_update_source_component(
        self, prefix: str, distribution: str, component: str, spec=dict
    ) -> dict:
        status_code, ret = self.api_put(
            "publish/"
            + urllib.parse.quote(prefix, safe="")
            + "/"
            + urllib.parse.quote(distribution, safe="")
            + "/sources/"
            + urllib.parse.quote(component, safe=""),
            data=json.dumps(spec),
            headers={"Content-Type": "application/json"},
        )
        logger.debug("api_publish_update_source_component returned %d", status_code)
        return ret

    def api_publish_remove_source_component(
        self, prefix: str, distribution: str, component: str
    ) -> int:
        status_code, _ = self.api_delete(
            "publish/"
            + urllib.parse.quote(prefix, safe="")
            + "/"
            + urllib.parse.quote(distribution, safe="")
            + "/sources/"
            + urllib.parse.quote(component, safe="")
        )
        logger.debug("api_publish_remove_source_component returned %d", status_code)
        return status_code

    def api_publish_update_published_repository(
        self, prefix: str, distribution: str, spec=dict
    ) -> dict:
        status_code, ret = self.api_post(
            "publish/"
            + urllib.parse.quote(prefix, safe="")
            + "/"
            + urllib.parse.quote(distribution, safe="")
            + "/update",
            data=json.dumps(spec),
            headers={"Content-Type": "application/json"},
        )
        logger.debug("api_publish_update_published_repository returned %d", status_code)
        return ret

    ## CLEANUP
    #############

    def api_db_cleanup(self) -> Optional[Any]:
        status_code, ret = self.api_post("db/cleanup")
        logger.debug("api_db_cleanup returned %d", status_code)
        return ret

    ## TASKS
    ###########

    def api_tasks(self) -> Optional[Any]:
        status_code, ret = self.api_get("tasks")
        logger.debug("api_tasks returned %d", status_code)
        return ret

    def api_task_delete(self, tid: int) -> Optional[Any]:
        status_code, _ = self.api_delete(f"tasks/{tid}")
        logger.debug("api_tasks_delete returned %d", status_code)
        return status_code

    def api_task_output(self, tid: int) -> Optional[Any]:
        status_code, ret = self.api_get(f"tasks/{tid}/output")
        logger.debug("api_tasks_output returned %d", status_code)
        return ret

    def api_task_detail(self, tid: int) -> Optional[Any]:
        status_code, ret = self.api_get(f"tasks/{tid}/detail")
        logger.debug("api_tasks_detail returned %d", status_code)
        return ret

    def api_tasks_clear(self) -> Optional[Any]:
        status_code, ret = self.api_post("tasks-clear")
        logger.debug("api_tasks_clear returned %d", status_code)
        return ret


class AptlyManager:
    def __init__(self, verbosity: int):
        super().__init__()
        self.verbosity = verbosity
        self.aptly_via_api = AptlyApiClient()

    def refresh_mirror_list(self) -> None:
        self.mirrors = [m["Name"] for m in self.aptly_via_api.api_mirror_list()]

    def refresh_repo_list(self) -> None:
        repos = set()
        for i in self.aptly_via_api.api_repos_list():
            repos.add(i["Name"])
        self.repos = sorted(list(repos))

    def refresh_published_repo_list(self) -> None:
        self.published_repos = [m for m in self.aptly_via_api.api_publish_list()]

    def update_mirrors(self) -> None:
        self.refresh_mirror_list()

        for m in self.mirrors:
            if self.verbosity >= 2:
                logger.warning("Updating mirror %s", m)
            self.aptly_via_api.api_mirror_update(m)
            if self.verbosity >= 1:
                logger.warning("Updated mirror %s", m)


def _gen_aptly_config() -> None:
    fspes = {}
    s3pes = {}
    _fill_publish_prefixes()
    for p in jennyconfig["publishprefixes"]:
        fspes[p] = {
            "linkMethod": "hardlink",
            "verifyMethod": "md5",
            "rootDir": jennyconfig["publishprefixes"][p],
        }
    for p in jennyconfig["publishes"]:
        if jennyconfig["publishes"][p]["type"] != "s3":
            continue
        s3pes[p] = {
            "debug": False,
            "endpoint": jennyconfig["publishes"][p]["endpoint"],
            "bucket": jennyconfig["publishes"][p]["bucket"],
            "region": "none",
            "awsAccessKeyID": jennyconfig["publishes"][p]["keyid"],
            "awsSecretAccessKey": jennyconfig["publishes"][p]["secretkey"],
        }
    c = {
        "rootDir": os.path.join(aptly_basedir, "aptly-data"),
        "downloadConcurrency": 4,
        "downloadSpeedLimit": 0,
        "downloadRetries": 0,
        "downloader": "default",
        "databaseOpenAttempts": -1,
        "architectures": [],
        "dependencyFollowSuggests": False,
        "dependencyFollowRecommends": False,
        "dependencyFollowAllVariants": False,
        "dependencyFollowSource": False,
        "dependencyVerboseResolve": False,
        "gpgDisableSign": False,
        "gpgDisableVerify": False,
        "gpgProvider": "gpg",
        "downloadSourcePackages": False,
        "skipLegacyPool": True,
        "ppaDistributorID": "ubuntu",
        "ppaCodename": "",
        "skipContentsPublishing": True,
        "skipBz2Publishing": True,
        "SwiftPublishEndpoints": {},
        "AzurePublishEndpoints": {},
        "AsyncAPI": False,
        "enableMetricsEndpoint": False,
        "FileSystemPublishEndpoints": fspes,
        "S3PublishEndpoints": s3pes,
    }

    with open(aptly_config_file, mode="w") as f:
        json.dump(c, f, indent=2)


def _create_aptly_repos(asyncpub=False) -> None:
    am.refresh_repo_list()
    am.refresh_mirror_list()
    am.refresh_published_repo_list()
    impacted_dists = set()
    for d in jennyconfig["dists"]:
        c = jennyconfig["dists"][d]
        for comp in c["components"]:
            mname = dec2str(c["basename"], c["env"], comp)
            if c["ismirror"]:
                spec = {
                    "Name": mname,
                    "ArchiveURL": c["upstream"],
                    "Distribution": c["suite"],
                    "Components": [comp],
                    "Architectures": c["binary-architectures"],
                    "DownloadSources": c["has-sources"],
                    "DownloadUdebs": c["udebs"] if "udebs" in c else False,
                }
                # Workaround for packages being referenced in Sources but not actually present
                spec["Filter"] = "!Extra-Source-Only (yes)"
                if "filterformula" in c:
                    spec["Filter"] += ", (" + c["filterformula"] + ")"
                if mname not in am.mirrors:
                    logger.warning("Need to create mirror %s", mname)
                    am.aptly_via_api.api_mirror_create(spec)
                    for env, params in jennyconfig["publishes"].items():
                        if c["basename"] in params["dists"]:
                            logger.warning(
                                "Scheduling publication mirror %s/%s",
                                env,
                                c["basename"],
                            )
                            impacted_dists.add(de2str(c["basename"], env))
                else:
                    logger.warning("Mirror %s already exists", mname)
                    current_spec = am.aptly_via_api.api_mirror_get(mname)
                    need_update = False
                    for field in spec.copy():
                        if field == "ArchiveURL":
                            if spec["ArchiveURL"] != current_spec["ArchiveRoot"]:
                                logger.warning(
                                    "ArchiveURL field in mirror configuration %s changed",
                                    mname,
                                )
                                need_update = True
                                break
                        else:
                            if type(field) is list:
                                current_spec[field] = set(current_spec[field])
                                spec[field] = set(spec[field])
                            if current_spec[field] != spec[field]:
                                logger.warning(
                                    "%s field in mirror configuration %s changed",
                                    field.capitalize(),
                                    mname,
                                )
                                need_update = True
                                break
                    if need_update:
                        logger.warning("Need to update mirror %s", mname)
                        am.aptly_via_api.api_mirror_update(mname, spec)
                        for env, params in jennyconfig["publishes"].items():
                            if c["basename"] in params["dists"]:
                                logger.warning(
                                    "Scheduling publication repo %s/%s",
                                    env,
                                    c["basename"],
                                )
                                impacted_dists.add(de2str(c["basename"], env))
            else:
                info = {
                    "Name": mname,
                    "Comment": c["description"] if "description" in c else "",
                    "DefaultDistribution": "",
                    "DefaultComponent": "",
                }
                if mname not in am.repos:
                    logger.warning("Need to create repo %s", mname)
                    am.aptly_via_api.api_repos_create(info)
                    for env, params in jennyconfig["publishes"].items():
                        if c["basename"] in params["dists"]:
                            logger.warning(
                                "Scheduling publication repo %s/%s", env, c["basename"]
                            )
                            impacted_dists.add(de2str(c["basename"], env))
                else:
                    logger.warning("Repo %s already exists", mname)
                    # TODO after https://github.com/aptly-dev/aptly/issues/1453 issue will be solved
                    # current_info = am.aptly_via_api.api_repos_get(mname)
                    # need_update=False
                    # for field in info:
                    #     if current_info[field] != info[field]:
                    #         logger.warning("%s field in repo configuration %s has changed", field.capitalize(), mname)
                    #         need_update=True
                    #         break
                    # if need_update:
                    #     logger.warning("Need to update repo %s", mname)
                    #     am.aptly_via_api.api_repos_update(mname, info)
                    #     for env, params in jennyconfig['publishes']:
                    #         if mname in params['dists']:
                    #             impacted_dists.add(de2str(c["basename"], env))

    # Clean not used mirrors and published repo linked
    activated_mirrors = [
        dec2str(v["basename"], v["env"], component)
        for k, v in jennyconfig["dists"].items()
        if v["ismirror"]
        for component in v["components"]
    ]
    for m in am.mirrors:
        if m not in activated_mirrors:
            logger.warning("Delete not used mirror %s", m)
            am.aptly_via_api.api_mirror_delete(m, force=True)
            for env, params in jennyconfig["publishes"].items():
                distribution, _, component = str2dec(m)
                if distribution in params["dists"]:
                    logger.warning(
                        "Scheduling publication mirror %s/%s", env, distribution
                    )
                    impacted_dists.add(de2str(distribution, env))

    # Clean not used repositories and published repo linked
    activated_repos = [
        dec2str(v["basename"], v["env"], component)
        for k, v in jennyconfig["dists"].items()
        if not v["ismirror"]
        for component in v["components"]
    ]
    for m in am.repos:
        if m not in activated_repos:
            logger.warning("Delete not used repo %s", m)
            am.aptly_via_api.api_repo_delete(m, force=True)
            for env, params in jennyconfig["publishes"].items():
                distribution, _, component = str2dec(m)
                if distribution in params["dists"]:
                    logger.warning(
                        "Scheduling publication repo %s/%s", env, distribution
                    )
                    impacted_dists.add(de2str(distribution, env))

    for dist in sorted(list(impacted_dists)):
        backend_publish_dist(dist, asyncpub=asyncpub)

    # Clean not used published repository
    activated_published_repositories = [
        de2str(dist, v["prefix"])
        for v in jennyconfig["publishes"].values()
        for dist in v["dists"]
    ]
    for pr in am.published_repos:
        if (
            de2str(pr["Distribution"], pr["Prefix"])
            not in activated_published_repositories
        ):
            logger.warning(
                "Delete not used published repo %s for %s",
                pr["Distribution"],
                pr["Prefix"],
            )
            am.aptly_via_api.api_publish_delete(
                pr["Storage"] + ":" + pr["Prefix"], pr["Distribution"]
            )


def backend_init() -> None:
    global am
    _gen_aptly_config()
    am = AptlyManager(5)
    _create_aptly_repos(asyncpub=True)


def _backend_update_mirror(mname) -> None:
    am.aptly_via_api.api_mirror_update(mname)


def backend_update_mirrors(asyncpub=False) -> None:
    impacted_dists = set()
    for d in jennyconfig["dists"]:
        if not jennyconfig["dists"][d]["ismirror"]:
            continue
        c = jennyconfig["dists"][d]
        for comp in c["components"]:
            mname = dec2str(c["basename"], c["env"], comp)
            _backend_update_mirror(mname)
        impacted_dists.add(de2str(c["basename"], c["env"]))
    for dist in sorted(list(impacted_dists)):
        backend_publish_dist(dist, asyncpub=asyncpub)


def _backend_read_packages(
    distribution, snap: str = None, q: str = None
) -> dict[str, list[AptlyPackageEntry]]:
    d = {}
    for comp in jennyconfig["dists"][distribution]["components"]:
        fulldist = dec2str(
            jennyconfig["dists"][distribution]["basename"],
            jennyconfig["dists"][distribution]["env"],
            comp,
        )
        if snap:
            fullsnap = f"{fulldist}_snapfor_{snap}"
            data = am.aptly_via_api.api_snapshots_packages(
                fullsnap,
                {"q": q, "format": "details"},
            )
        elif jennyconfig["dists"][distribution]["ismirror"]:
            data = am.aptly_via_api.api_mirror_packages(
                fulldist,
                {"q": q, "format": "details"},
            )
        else:
            data = am.aptly_via_api.api_repos_packages(
                fulldist,
                {"q": q, "format": "details"},
            )
        for i in data:
            try:
                pe = _parsetope(i["Key"], fulldist, comp)
            except TypeError:
                continue
            if "Source" in i:
                source = re.sub(" .*", "", i["Source"])
            else:
                source = pe.name
            if source not in d:
                d[source] = []
            d[source].append(pe)
    return d


def backend_read_packages(
    distribution, snap: str = None, q: str = None
) -> list[AptlyPackageEntry]:
    d = _backend_read_packages(distribution, snap, q)
    res = []

    for s in sorted(d.keys()):
        res.extend(sorted(d[s], key=PackageEntry.sortablestr))
    return res


def backend_read_packages_grouped(
    distribution, snap: str = None, q: str = None
) -> dict[str, list[AptlyPackageEntry]]:
    d = _backend_read_packages(distribution, snap, q)
    res = {}

    for sp in sorted(d.keys()):
        res[sp] = d[sp]
        res[sp].sort(key=PackageEntry.sortablestr)
    return res


def _backend_diff_snaps(sn1, leftdist, sn2, rightdist, comp):
    onlyleft = []
    onlyright = []
    diffver = []
    key2pe = {}
    key2source = {}
    diff = am.aptly_via_api.api_snapshots_diff(sn1, sn2)
    for d in diff:
        if d == "error":
            continue
        if d["Left"]:
            cleft = _parsetope(d["Left"], leftdist, comp)
            key2pe[d["Left"]] = cleft
        else:
            cleft = None
        if d["Right"]:
            cright = _parsetope(d["Right"], rightdist, comp)
            key2pe[d["Right"]] = cright
        else:
            cright = None

        if cleft:
            if cright:
                diffver.append([cleft, cright])
            else:
                onlyleft.append(cleft)
        else:
            if cright:
                onlyright.append(cright)
            else:
                # Neither left nor right???
                pass

    fl1 = {
        "(Name (= %(pname)s),($PackageType (= source)))" % {"pname": key2pe[k].name}
        for k in key2pe
        if key2pe[k].architecture == "source"
    }
    fl2 = {
        "($Source (= %(pname)s),($PackageType (= deb)|$PackageType (= udeb)))|(Name (= %(pname)s))"
        % {"pname": key2pe[k].name}
        for k in key2pe
        if key2pe[k].architecture != "source"
    }
    fl = list(fl1 | fl2)
    while fl:
        sub = fl[:batchsize]
        formula = "|".join(sub)
        for s in [sn1, sn2]:
            tmp = am.aptly_via_api.api_snapshots_packages(
                s,
                {
                    "q": formula,
                    "format": "details",
                },
            )
            if not tmp:
                continue
            for i in tmp:
                if "Source" in i:
                    key2source[i["Key"]] = re.sub(" .*", "", i["Source"])
                elif "Name" in i:
                    key2source[i["Key"]] = i["Name"]
                else:
                    key2source[i["Key"]] = i["Package"]
        fl = fl[batchsize:]

    return {
        "onlyleft": onlyleft,
        "onlyright": onlyright,
        "diffver": diffver,
        "key2pe": key2pe,
        "key2source": key2source,
    }


def backend_diff_dists(
    dist: str, leftenv: str, leftsnap: str, rightenv: str, rightsnap: str
) -> dict:
    leftdist = de2str(dist, leftenv)
    rightdist = de2str(dist, rightenv)
    c = jennyconfig["dists"][leftdist]

    components = c["components"]
    onlyleft = []
    onlyright = []
    diffver = []
    suffix = str(uuid.uuid4())
    key2source = {}
    for comp in components:
        if leftsnap:
            sn1 = "%s_snapfor_%s" % (dec2str(dist, leftenv, comp), leftsnap)
        else:
            sn1 = "%s_%s_tmpfordiff" % (dec2str(dist, leftenv, comp), suffix)
            if jennyconfig["dists"][leftdist]["ismirror"]:
                am.aptly_via_api.api_mirror_snapshot(dec2str(dist, leftenv, comp), sn1)
            else:
                am.aptly_via_api.api_repos_snapshot(dec2str(dist, leftenv, comp), sn1)
        if rightsnap:
            sn2 = "%s_snapfor_%s" % (dec2str(dist, rightenv, comp), rightsnap)
        else:
            sn2 = "%s_%s_tmpfordiff" % (dec2str(dist, rightenv, comp), suffix)
            if jennyconfig["dists"][rightdist]["ismirror"]:
                am.aptly_via_api.api_mirror_snapshot(dec2str(dist, rightenv, comp), sn2)
            else:
                am.aptly_via_api.api_repos_snapshot(dec2str(dist, rightenv, comp), sn2)

        ###
        diffdata = _backend_diff_snaps(sn1, leftdist, sn2, rightdist, comp)
        onlyleft += diffdata["onlyleft"]
        onlyright += diffdata["onlyright"]
        diffver += diffdata["diffver"]
        key2source |= diffdata["key2source"]
        if not leftsnap:
            am.aptly_via_api.api_snapshots_delete(sn1)
        if not rightsnap:
            am.aptly_via_api.api_snapshots_delete(sn2)

    res = {
        "onlyleft": onlyleft,
        "onlyright": onlyright,
        "diffver": diffver,
        "dist": dist,
        "leftenv": leftenv,
        "rightenv": rightenv,
        "leftdist": leftdist,
        "rightdist": rightdist,
        "key2source": key2source,
    }
    return res


def backend_diff_dists_grouped(
    dist: str,
    leftenv: str,
    leftsnap: str,
    rightenv: str,
    rightsnap: str,
    package_filter: list[str] = None,
) -> dict:
    diffdata = backend_diff_dists(dist, leftenv, leftsnap, rightenv, rightsnap)

    pmap = diffdata["key2source"]

    diffdata_grouped = {}
    for only in ["onlyleft", "onlyright"]:
        diffdata_grouped[only] = {}
        for i in diffdata[only]:
            if i.key in pmap:
                source_package = pmap[i.key]
            else:
                source_package = i.name
            if package_filter and (source_package not in package_filter):
                continue
            if source_package not in diffdata_grouped[only]:
                diffdata_grouped[only][source_package] = []
            diffdata_grouped[only][source_package].append(i)

    diffdata_grouped["diffver"] = {}
    for i in diffdata["diffver"]:
        source_package = pmap[i[0].key]
        if package_filter and (source_package not in package_filter):
            continue
        if source_package not in diffdata_grouped["diffver"]:
            diffdata_grouped["diffver"][source_package] = []
        diffdata_grouped["diffver"][source_package].append(i)

    for k in diffdata:
        if k not in diffdata_grouped:
            diffdata_grouped[k] = diffdata[k]

    diffdict = {}
    for side in ["left", "right"]:
        only = f"only{side}"
        for sourcepkg, data in diffdata_grouped[only].items():
            if sourcepkg not in diffdict:
                diffdict[sourcepkg] = {}
            for binpkg in data:
                pv = f"{binpkg.name}/{binpkg.architecture}/{binpkg.component}"
                if pv not in diffdict[sourcepkg]:
                    diffdict[sourcepkg][pv] = {
                        "name": binpkg.name,
                        "architecture": binpkg.architecture,
                        "component": binpkg.component,
                    }
                if side not in diffdict[sourcepkg][pv]:
                    diffdict[sourcepkg][pv][side] = set()
                diffdict[sourcepkg][pv][side].add(binpkg.version)

    for sourcepkg, data in diffdata_grouped["diffver"].items():
        if package_filter and (sourcepkg not in package_filter):
            continue
        if sourcepkg not in diffdict:
            diffdict[sourcepkg] = {}
        for binlist in data:
            binpkg = binlist[0]
            pv = f"{binpkg.name}/{binpkg.architecture}/{binpkg.component}"
            for side in ["left", "right"]:
                if pv not in diffdict[sourcepkg]:
                    diffdict[sourcepkg][pv] = {
                        "name": binpkg.name,
                        "architecture": binpkg.architecture,
                        "component": binpkg.component,
                    }
                if side not in diffdict[sourcepkg][pv]:
                    diffdict[sourcepkg][pv][side] = set()
            diffdict[sourcepkg][pv]["left"].add(binlist[0].version)
            diffdict[sourcepkg][pv]["right"].add(binlist[1].version)

    diffdata_grouped["diffs"] = diffdict
    return diffdata_grouped


def backend_migrate_packages_expert(
    fromdist: str,
    fromenv: str,
    todist: str,
    toenv: str,
    tocomp: str,
    pkgs: list[str],
    asyncpub=False,
) -> None:
    backend_remove_packages(
        distribution=todist,
        environment=toenv,
        packages=pkgs,
        component=tocomp,
        publish=False,
        asyncpub=asyncpub,
    )
    while pkgs:
        formula = "|".join(
            [
                "($Source (= %(pname)s),($PackageType (= deb)|$PackageType (= udeb)))|(Name (= %(pname)s))"
                % {"pname": p}
                for p in pkgs[:batchsize]
            ]
        )
        plist = backend_read_packages(de2str(fromdist, fromenv), q=formula)
        keylist = [p.key for p in plist]
        while keylist:
            am.aptly_via_api.api_repos_add_packages(
                dec2str(todist, toenv, tocomp), {"PackageRefs": keylist[:batchsize]}
            )
            keylist = keylist[batchsize:]
        pkgs = pkgs[batchsize:]
    backend_publish_dist(de2str(todist, toenv), asyncpub=asyncpub)


def backend_migrate_packages(
    fromenv: str,
    fromsnap: str,
    toenv: str,
    srcpkgs: dict[str, list[str]] = dict,
    component: str = None,
    asyncpub=False,
) -> None:
    migrated = {}
    impacted_dists = set()
    for d in srcpkgs:
        migrated[d] = []
        fromdist = de2str(d, fromenv)
        backend_remove_packages(
            distribution=d,
            environment=toenv,
            packages=srcpkgs[d],
            component=component,
            publish=False,
            asyncpub=asyncpub,
        )
        impacted_dists.add(de2str(d, toenv))
        srcpkgs[d] = list(srcpkgs[d])
        while srcpkgs[d]:
            formula = "|".join(
                [
                    "($Source (= %(pname)s),($PackageType (= deb)|$PackageType (= udeb)))|(Name (= %(pname)s))"
                    % {"pname": p}
                    for p in srcpkgs[d][:batchsize]
                ]
            )
            plist = backend_read_packages(fromdist, fromsnap, q=formula)
            plistbycomp = {}
            for p in plist:
                if component and p.component != component:
                    continue
                if p.component not in plistbycomp:
                    plistbycomp[p.component] = []
                plistbycomp[p.component].append(p)
            for comp in plistbycomp:
                crit = [p.key for p in plistbycomp[comp]]
                while crit:
                    am.aptly_via_api.api_repos_add_packages(
                        dec2str(d, toenv, comp), {"PackageRefs": crit[:batchsize]}
                    )
                    crit = crit[batchsize:]
                migrated[d].extend(plistbycomp[comp])
            srcpkgs[d] = srcpkgs[d][batchsize:]

    for dist in sorted(list(impacted_dists)):
        backend_publish_dist(dist, asyncpub=asyncpub)
    return migrated


def backend_migrate_all_packages(
    fromenv: str,
    toenv: str,
    dists: list[str],
    component: str = None,
    asyncpub=False,
) -> None:
    migrated = {}
    impacted_dists = set()
    for d in dists:
        migrated[d] = []
        fromdist = de2str(d, fromenv)
        backend_remove_packages(
            distribution=d,
            environment=toenv,
            packages=["*"],
            component=component,
            asyncpub=asyncpub,
        )
        impacted_dists.add(de2str(d, toenv))
        plistfrom = backend_read_packages(fromdist)
        plistbycomp = {}
        for p in plistfrom:
            if component and p.component != component:
                continue
            if p.component not in plistbycomp:
                plistbycomp[p.component] = []
            plistbycomp[p.component].append(p)
        for comp in plistbycomp:
            crit = [p.key for p in plistbycomp[comp]]
            while crit:
                am.aptly_via_api.api_repos_add_packages(
                    dec2str(d, toenv, comp), {"PackageRefs": crit[:batchsize]}
                )
                crit = crit[batchsize:]
            migrated[d].extend(plistbycomp[comp])

    for dist in sorted(list(impacted_dists)):
        backend_publish_dist(dist, asyncpub=asyncpub)
    return migrated


def backend_remove_packages(
    distribution: str,
    environment: str,
    packages: list[str],
    component: str = None,
    publish: bool = True,
    asyncpub=False,
) -> list[PackageEntry]:
    if "*" in packages:
        formula = "$PackageType (= deb)|$PackageType (= udeb)|$PackageType (= source)"
    else:
        formula = "|".join(
            [
                "($Source (= %(pname)s),($PackageType (= deb)|$PackageType (= udeb)))|(Name (= %(pname)s),$PackageType (= source))"
                % {"pname": package}
                for package in packages
            ]
        )
    return backend_remove_packages_from_formula(
        distribution, environment, formula, component, publish, asyncpub=asyncpub
    )


def backend_remove_packages_from_formula(
    distribution: str,
    environment: str,
    formula: str,
    component: str = None,
    publish: bool = True,
    asyncpub=False,
) -> list[PackageEntry]:
    dist = de2str(distribution, environment)
    plist = backend_read_packages(dist, q=formula)
    plistbycomp = {}
    for p in plist:
        if component and p.component != component:
            continue
        if p.component not in plistbycomp:
            plistbycomp[p.component] = []
        plistbycomp[p.component].append(p)
    removed = []
    for comp in plistbycomp:
        crit = [p.key for p in plistbycomp[comp]]
        while crit:
            am.aptly_via_api.api_repos_delete_packages(
                dec2str(distribution, environment, comp),
                {"PackageRefs": crit[:batchsize]},
            )
            crit = crit[batchsize:]
        removed.extend(plistbycomp[comp])
    if publish:
        backend_publish_dist(dist, asyncpub=asyncpub)
    return removed


def backend_empty_repository(
    distribution: str, environment: str, asyncpub=False
) -> list[PackageEntry]:
    return backend_remove_packages_from_formula(
        distribution, environment, "Name", asyncpub=asyncpub
    )


def backend_include_changes(dist: str, changes: str, asyncpub=False) -> None:
    c = jennyconfig["dists"][dist]
    distcomponents = c["components"]
    if c["ismirror"]:
        return

    distribution, environment = str2de(dist)

    packagedir = os.path.dirname(changes)
    packagefiles = {os.path.basename(changes): open(changes, mode="rb")}
    for stanza in deb822.Packages.iter_paragraphs(open(changes)):
        if "Files" in stanza:
            for line in stanza["Files"].split("\n"):
                line = line.strip()
                if not line:
                    continue
                filename = line.split(" ")[4]
                packagefiles[filename] = open(
                    os.path.join(packagedir, filename), mode="rb"
                )
                if filename.endswith(".deb") or filename.endswith(".udeb"):
                    control = debfile.DebFile(
                        os.path.join(packagedir, filename)
                    ).debcontrol()
                    if "Section" in control:
                        component = (
                            control["Section"].split("/")[0]
                            if control["Section"].split("/")[0] in distcomponents
                            else distcomponents[0]
                        )
        if "Source" in stanza:
            backend_remove_packages(
                distribution=c["basename"],
                environment=c["env"],
                packages=[stanza["Source"]],
                component=component,
                publish=False,
            )
    tmp = str(uuid.uuid4())
    am.aptly_via_api.api_files_upload(tmp, files=packagefiles)
    dec = dec2str(distribution, environment, component)
    am.aptly_via_api.api_repos_add_from_upload(dec, tmp)
    am.aptly_via_api.api_files_delete_dir(tmp)
    for filename in packagefiles:
        os.unlink(os.path.join(packagedir, filename))

    backend_publish_dist(dist, asyncpub=asyncpub)


def backend_include_debs(dist: str, debs: list[str], asyncpub=False) -> None:
    c = jennyconfig["dists"][dist]
    distcomponents = c["components"]
    if c["ismirror"]:
        return
    distribution, environment = str2de(dist)
    packagefiles = {}
    tmp = str(uuid.uuid4())
    removesets = {}
    for deb in debs:
        packagefiles = {os.path.basename(deb): open(deb, mode="rb")}
        # Search component in debian/control or set to first component of distribution
        control = debfile.DebFile(deb).debcontrol()
        if "Section" in control:
            component = (
                control["Section"].split("/")[0]
                if control["Section"].split("/")[0] in distcomponents
                else distcomponents[0]
            )
            target = f"{tmp}-{component}"
            if component not in removesets:
                removesets[component] = set()
        if "Source" in control:
            # The Source: field may contain a version number, strip it
            removesets[component].add(re.sub(" .*", "", control["Source"]))
        elif "Package" in control:
            removesets[component].add(control["Package"])
        am.aptly_via_api.api_files_upload(target, files=packagefiles)

    for component in removesets:
        backend_remove_packages(
            distribution=c["basename"],
            environment=c["env"],
            packages=removesets[component],
            component=component,
            publish=False,
        )
        dec = dec2str(distribution, environment, component)
        target = f"{tmp}-{component}"
        am.aptly_via_api.api_repos_add_from_upload(dec, target)
        am.aptly_via_api.api_files_delete_dir(target)

    for deb in debs:
        packagedir = os.path.dirname(deb)
        os.unlink(os.path.join(packagedir, deb))

    backend_publish_dist(dist, asyncpub=asyncpub)


def backend_incoming_daemon() -> None:
    am.refresh_repo_list()
    i = inotify.adapters.Inotify()
    watched = {}
    for d in jennyconfig["dists"]:
        c = jennyconfig["dists"][d]
        if c["ismirror"]:
            continue
        incomingdir = os.path.join(jenny_basedir, "incoming", d)
        os.makedirs(incomingdir, exist_ok=True)
        i.add_watch(incomingdir)
        watched[incomingdir] = d
        logger.warning("Watching %s", incomingdir)
        for changesfile in glob.glob(os.path.join(incomingdir, "*.changes")):
            backend_include_changes(d, changesfile)
            logger.warning("Included %s in %s", changesfile, d)

    for event in i.event_gen(yield_nones=False):
        _, type_names, path, filename = event

        if path not in watched:
            continue
        if not filename.endswith(".changes"):
            continue
        dist = watched[path]
        changesfile = os.path.join(path, filename)
        if not os.path.exists(changesfile):
            continue
        if set(type_names) & {
            "IN_CLOSE_WRITE",
            "IN_CLOSE_NOWRITE",
            "IN_MOVED_TO",
            "IN_CREATE",
        }:
            backend_include_changes(dist, changesfile, asyncpub=False)
            logger.warning("Included %s in %s", changesfile, dist)


def backend_publish_dist(dist: str, asyncpub=False) -> None:
    logger.warning("backend_publish_dist (%s))", dist)
    for target_name, target_data in jennyconfig["publishes"].items():
        for target_dist in target_data["dists"]:
            if dist == de2str(target_dist, target_data["env"]):
                backend_publish(target_name, [target_dist], asyncpub=asyncpub)


def backend_publish(target: str, sources: list[str] = None, asyncpub=False) -> None:
    logger.warning("backend_publish %s/%s", target, sources)
    _fill_publish_prefixes()
    if target == "all":
        for t in jennyconfig["publishes"]:
            backend_publish(t, asyncpub=asyncpub)
        return

    pub = jennyconfig["publishes"][target]

    suffix = str(uuid.uuid4())
    if not sources:
        sources = pub["dists"]
    for s in sources:
        source = de2str(s, pub["env"])
        if source not in jennyconfig["dists"]:
            continue
        logger.warning("backend_publish for dist %s", s)
        c = jennyconfig["dists"][source]
        snapnames = {}
        for comp in c["components"]:
            mname = dec2str(s, pub["env"], comp)
            snap = "%s_%s_tmpforpublish" % (dec2str(s, pub["env"], comp), suffix)
            snapnames[mname] = snap
            realsnap = "%s-snap-for-%s" % (mname, target)
            if jennyconfig["dists"][source]["ismirror"]:
                logger.warning("mirror %s", snap)
                statuscode, ret = am.aptly_via_api.api_mirror_snapshot(mname, snap)
                if statuscode == 400:
                    if "mirror not updated" in ret["error"]:
                        logger.warning("Need to update mirror %s", mname)
                        am.aptly_via_api.api_mirror_update(mname)
                        am.aptly_via_api.api_mirror_snapshot(mname, snap)
            else:
                logger.warning("repo %s", snap)
                print(am.aptly_via_api.api_repos_snapshot(mname, snap))

        if jennyconfig["publishes"][target]["type"] == "filesystem":
            prefix = "filesystem:%(prefix)s:%(target)s" % {
                "prefix": jennyconfig["publishes"][target]["publishprefix"],
                "target": target,
            }
        elif jennyconfig["publishes"][target]["type"] == "s3":
            prefix = "s3:%(target)s:%(prefix)s" % {
                "target": target,
                "prefix": jennyconfig["publishes"][target]["prefix"],
            }
        else:
            prefix = None
            raise KeyError
        distribution = s + pub["suffix"]
        sources = [
            {"Name": snapnames[dec2str(s, pub["env"], c)], "Component": c}
            for c in jennyconfig["dists"][source]["components"]
        ]
        spec = {
            "Signing": {
                "GpgKey": jennyconfig["publishes"][target]["signkey"],
                "Skip": not jennyconfig["publishes"][target]["signexports"],
            },
            "SkipContents": True,
        }
        if (
            "backports" in jennyconfig["dists"][source]
            and jennyconfig["dists"][source]["backports"]
        ):
            spec["NotAutomatic"] = "yes"
            spec["ButAutomaticUpgrades"] = "yes"
        spec["Architectures"] = jennyconfig["dists"][source]["architectures"]
        spec["Distribution"] = distribution
        spec["Sources"] = sources
        spec["SourceKind"] = "snapshot"
        spec["Snapshots"] = sources
        for comp in c["components"]:
            mname = dec2str(c["basename"], c["env"], comp)
            snap = snapnames[mname]
            realsnap = "%s-snap-for-%s" % (mname, target)
            renamedsnap = f"{realsnap}-tmpfordrop-{suffix}"
            logger.warning("start snapshot update for %s", s)
            am.aptly_via_api.api_snapshots_update(realsnap, {"Name": renamedsnap})
            logger.warning("snapshot update ok for %s", s)
        publish = am.aptly_via_api.api_publish_get(prefix, distribution)
        if publish:
            published_components = set(
                {source["Component"] for source in publish["Sources"]}
            )
            logger.warning("%s/%s published repo exists", c["env"], distribution)
            if set(c["components"]) != set(published_components):
                logger.warning("start replace source components for %s", s)
                am.aptly_via_api.api_publish_replace_source_components(
                    prefix, distribution, sources
                )
                am.aptly_via_api.api_publish_update_published_repository(
                    prefix, distribution, spec
                )
            need_update = False
            field_changed = set()
            for field in spec.keys():
                # Signing field is not present in GET /api/publish response
                # The Sources and Snapshots fields may change with each publication.
                if field not in ("Signing", "Snapshots", "Sources"):
                    if isinstance(spec[field], list):
                        if set(spec[field]) != set(publish[field]):
                            field_changed.add(field)
                            need_update = True

                    elif isinstance(spec[field], str) or isinstance(spec[field], bool):
                        if spec[field] != publish[field]:
                            field_changed.add(field)
                            need_update = True
                    else:
                        logger.warning(
                            "%s type unmanaged for %s field", type(spec[field]), field
                        )

            # We are forced to delete the publish repo and recreate it because there is no API route to update it (to date).
            if need_update:
                logger.warning("%s field(s) changed on publish %s", field_changed, s)
                logger.warning("Drop publish %s", s)
                am.aptly_via_api.api_publish_delete(prefix, distribution, force=True)
                logger.warning("start publish create for %s", s)
                am.aptly_via_api.api_publish_create(prefix, spec, asyncpub=asyncpub)
            logger.warning("start publish update for %s", s)
            status = None
            status = am.aptly_via_api.api_publish_update(
                prefix, distribution, spec, asyncpub=asyncpub
            )
            if status not in {200, 202}:
                logger.warning("publish update not ok for %s, error: %d", s, status)
                if not pub["ignore-errors"]:
                    raise HTTPException(
                        "publish update not ok for %s, error: %d" % (s, status)
                    )
            else:
                logger.warning("publish update ok for %s", s)
        else:
            logger.warning("start publish create for %s", s)
            status = am.aptly_via_api.api_publish_create(
                prefix, spec, asyncpub=asyncpub
            )
            if status in {201, 202}:
                logger.warning("publish create ok for %s", s)
            else:
                logger.warning("publish create not ok for %s, error: %d", s, status)
                if not pub["ignore-errors"]:
                    raise HTTPException(
                        "publish create not ok for %s, error: %d" % (s, status)
                    )
        for comp in c["components"]:
            mname = dec2str(c["basename"], c["env"], comp)
            snap = snapnames[mname]
            realsnap = "%s-snap-for-%s" % (mname, target)
            renamedsnap = f"{realsnap}-tmpfordrop-{suffix}"
            try:
                logger.warning("start snapshot delete for %s", s)
                am.aptly_via_api.api_snapshots_delete(renamedsnap)
                logger.warning("snapshot delete ok for %s", s)
            except ApiException:
                logger.warning("snapshot delete not ok for %s", s)
                # raise
            logger.warning("start snapshot update for %s", s)
            spec = {"Name": realsnap}
            am.aptly_via_api.api_snapshots_update(snap, spec)
            logger.warning("snapshot update ok for %s", s)


def backend_fill_distribution_from_source(
    distribution: str, environment: str, url: str, suite: str, asyncpub=False
) -> None:
    logger.warning(
        "Filling distribution %s from url %s with suite %s", distribution, url, suite
    )
    logger.warning("First, emptying target repository %s/%s", distribution, environment)
    backend_empty_repository(distribution, environment)
    d = de2str(distribution, environment)
    c = jennyconfig["dists"][d]
    am.refresh_mirror_list()
    for comp in c["components"]:
        mname = dec2str(distribution, environment, comp)
        if mname not in am.mirrors:
            logger.warning("Need to create mirror %s", mname)
            spec = {
                "Name": mname,
                "ArchiveURL": url,
                "Distribution": suite,
                "Components": [comp],
                "Architectures": c["binary-architectures"],
                "DownloadSources": c["has-sources"],
                "DownloadUdebs": c["udebs"] if "udebs" in c else False,
            }
            # Workaround for packages being referenced in Sources but not actually present
            spec["Filter"] = "!Extra-Source-Only (yes)"

            res = am.aptly_via_api.api_mirror_create(spec)
            if "error" in res:
                logger.warning(res)
                logger.warning("Skipping")
                sys.exit(1)
                continue
        else:
            logger.warning("Mirror %s already exists", mname)
        logger.warning("Updating mirror %s", mname)
        _backend_update_mirror(mname)
        logger.warning("Getting package list from %s", mname)
        data = am.aptly_via_api.api_mirror_packages(mname, spec={})
        logger.debug(data)
        logger.warning("Migrating packages from %s", mname)
        while data:
            res = am.aptly_via_api.api_repos_add_packages(
                dec2str(distribution, environment, comp),
                {"PackageRefs": data[:batchsize]},
            )
            data = data[batchsize:]
        logger.warning("Dropping temp mirror %s", mname)
        backend_publish_dist(d, asyncpub=asyncpub)
        am.aptly_via_api.api_mirror_delete(mname)


def backend_search_package(
    package: str,
    dists: list[str] = None,
    envs: list[str] = None,
    stages: list[str] = None,
) -> dict[str, list[AptlyPackageEntry]]:
    res = {}
    spec = {
        "q": f"(Name (= {package}))|($Source (= {package}))",
    }

    for d in jennyconfig["dists"]:
        c = jennyconfig["dists"][d]
        if dists and c["basename"] not in dists:
            continue
        if envs and c["env"] not in envs:
            continue
        if stages and not (set(stages) & set(c["stages"])):
            continue
        for comp in c["components"]:
            mname = dec2str(c["basename"], c["env"], comp)
            if c["ismirror"]:
                # for i in am.aptly_via_api.api_mirror_packages(mname, spec=spec):
                #    logger.warning("%s -- %s -- %s", i, d, comp)
                try:
                    r = [
                        _parsetope(i, d, comp)
                        for i in am.aptly_via_api.api_mirror_packages(mname, spec=spec)
                    ]
                except ValueError:
                    r = []
            else:
                # for i in am.aptly_via_api.api_repos_packages(mname, spec=spec):
                #    logger.warning("%s -- %s -- %s", i, d, comp)
                try:
                    r = [
                        _parsetope(i, d, comp)
                        for i in am.aptly_via_api.api_repos_packages(mname, spec=spec)
                    ]
                except ValueError:
                    r = []
            if r:
                key = (c["basename"], c["env"], comp)
                res[key] = r

    return res


def backend_drop_old_tmp_snapshots() -> None:
    for i in am.aptly_via_api.api_snapshots_list():
        name = i["Name"]
        if not re.search("_tmpfor", name):
            continue
        # There is also a datetime.datetime.fromisoformat() method,
        # but it only works with versions of Python more recent than
        # we currently have
        createdat = dateutil.parser.isoparse(i["CreatedAt"])
        if not createdat < datetime.datetime.now(
            datetime.timezone.utc
        ) - datetime.timedelta(days=7):
            continue
        logger.warning("Deleting snapshot %s", name)
        am.aptly_via_api.api_snapshots_delete(name)


def backend_drop_all_publish() -> None:
    for i in am.aptly_via_api.api_publish_list():
        print(i)
        distribution = i["Distribution"]
        prefix = i["Prefix"]
        storage = i["Storage"]
        logger.warning("Deleting published repo %s/%s", prefix, distribution)
        am.aptly_via_api.api_publish_delete(storage + ":" + prefix, distribution)


def backend_drop_upload_dirs() -> None:
    for d in am.aptly_via_api.api_files_list_dirs():
        logger.warning("Deleting upload dir %s", d)
        am.aptly_via_api.api_files_delete_dir(d)


def backend_add_package_from_files(
    distribution: str,
    environment: str,
    component: str,
    package: list[str],
    asyncpub=False,
) -> None:
    """$ curl -X POST http://localhost:8080/api/repos/repo1/file/aptly-0.9
    {"FailedFiles":[],"Report":{"Warnings":[],"Added":["aptly_0.9~dev+217+ge5d646c_i386 added"],"Removed":[]}}
    """
    dec = dec2str(distribution, environment, component)
    de = de2str(distribution, environment)
    if de not in jennyconfig["dists"]:
        raise KeyError(f"Unknown dist/env {distribution}/{environment}")
    if component not in jennyconfig["dists"][de]["components"]:
        raise KeyError(
            f"Unknown component {component} for dist/env {distribution}/{environment}"
        )
    if jennyconfig["dists"][de]["ismirror"]:
        raise RuntimeError("Can't insert packages into a mirror")
    tmp = str(uuid.uuid4())
    am.aptly_via_api.api_files_upload(
        tmp, files={os.path.basename(f): open(f, mode="rb") for f in package}
    )
    am.aptly_via_api.api_repos_add_from_upload(dec, tmp)
    backend_publish_dist(de, asyncpub=asyncpub)


def backend_list_snapshots() -> dict[str, dict[str, list[datetime.datetime]]]:
    snaps = {}
    for i in am.aptly_via_api.api_snapshots_list():
        name = i["Name"]
        m = re.search(r"(.*)_snapfor_(" + snapre + ")", name)
        if not m:
            continue
        name = m.group(2)
        _, env, _ = str2dec(m.group(1))
        createdat = dateutil.parser.isoparse(i["CreatedAt"]).astimezone(tz=tz)
        if env not in snaps:
            snaps[env] = {}
        if name in snaps[env]:
            snaps[env][name] = min(createdat, snaps[env][name])
        else:
            snaps[env][name] = createdat

    sortedsnaps = {}
    for env in snaps.keys():
        sortedsnaps[env] = {
            k: v
            for k, v in sorted(
                snaps[env].items(), key=lambda item: item[1], reverse=True
            )
        }
    return {
        env: sortedsnaps[env]
        for env in jennyconfig["environments"]
        if env in sortedsnaps
    }


def backend_create_snapshot(environment: str, name: str) -> None:
    _validate_snapshot_name(name)
    snaps = backend_list_snapshots()
    try:
        _ = snaps[environment][name]
        raise AlreadyExistsException(
            f"Snapshot {name} already exists for environment {environment}"
        )
    except KeyError:
        pass
    suffix = f"_snapfor_{name}"
    for de in jennyconfig["dists"]:
        dist, env = str2de(de)
        if env != environment:
            continue
        for comp in jennyconfig["dists"][de]["components"]:
            dec = dec2str(dist, environment, comp)
            sn = f"{dec}{suffix}"
            if jennyconfig["dists"][de]["ismirror"]:
                am.aptly_via_api.api_mirror_snapshot(dec, sn)
            else:
                am.aptly_via_api.api_repos_snapshot(dec, sn)


def backend_delete_snapshot(environment: str, name: str) -> None:
    _validate_snapshot_name(name)
    snaps = backend_list_snapshots()
    _ = snaps[environment][name]
    for i in am.aptly_via_api.api_snapshots_list():
        iname = i["Name"]
        m = re.search(r"(.*)_snapfor_(" + snapre + ")", iname)
        if not m:
            continue
        sname = m.group(2)
        _, senv, _ = str2dec(m.group(1))
        if sname == name and senv == environment:
            am.aptly_via_api.api_snapshots_delete(i["Name"])


def backend_cleanup() -> None:
    am.aptly_via_api.api_db_cleanup()


def backend_tasks() -> None:
    states = {
        0: "non lancée",
        1: "en cours",
        2: "succès",
        3: "échec",
    }
    tasks = []
    for t in am.aptly_via_api.api_tasks():
        # Update published snapshot repository filesystem:fs1:stable/trixie
        # The regex says:
        # anything, as long as possible, until a colon (named group "publishtype")
        # then anything, as long as possible, until another colon ("prefix")
        # then anything, as short as possible, until a slash ("publish")
        # then the rest ("dist"), which may include slashes
        if m := re.search(
            "Update published snapshot repository (?P<publishtype>.*):(?P<prefix>.*):(?P<publish>.*?)/(?P<dist>.*)",
            t["Name"],
        ):
            t["publishtype"] = m.group("publishtype")
            t["prefix"] = m.group("prefix")
            t["publish"] = m.group("publish")
            t["dist"] = m.group("dist")
        elif m := re.search(
            "Publish snapshot repository (?P<publishtype>.*):(?P<prefix>.*):(?P<publish>.*?)/(?P<dist>.*) with components",
            t["Name"],
        ):
            t["publishtype"] = m.group("publishtype")
            t["prefix"] = m.group("prefix")
            t["publish"] = m.group("publish")
            t["dist"] = m.group("dist")
        if "State" in t and t["State"] in states:
            t["statetext"] = states[t["State"]]
        else:
            t["statetext"] = "inconnue"
        tasks.append(t)
    tasks.reverse()
    return tasks


def backend_delete_task(tid: int):
    return am.aptly_via_api.api_task_delete(tid)


def backend_clear_tasks():
    return am.aptly_via_api.api_tasks_clear()


def backend_task_info(tid: int):
    return {
        "detail": am.aptly_via_api.api_task_detail(tid),
        "output": am.aptly_via_api.api_task_output(tid),
    }


am = AptlyManager(5)

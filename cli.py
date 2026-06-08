#! /usr/bin/python3

"""CLI frontend for Jenny

Feature set not yet on par with the web UI, but a couple of features
are only available in the CLI:
 * initialize the backend
 * update mirrors from their upstream source
 * include packages given explicitly
 * include packages found in an incoming directory
"""

import subprocess
import argh
import glob
from pprint import pprint
import platform
import os
import pwd
import re

from base import jenv
from base import urlsep, de2str, str2de
from jennylog import log_action
from config import jennyconfig
from backend import (
    backend_diff_dists_grouped,
    backend_read_packages_grouped,
    backend_include_changes,
    backend_include_debs,
    backend_migrate_packages,
    backend_remove_packages,
    backend_update_mirrors,
    backend_init,
    backend_incoming_daemon,
    backend_publish,
    backend_fill_distribution_from_source,
    backend_search_package,
    backend_drop_old_tmp_snapshots,
    backend_drop_upload_dirs,
    backend_add_package_from_files,
    backend_migrate_all_packages,
    backend_drop_all_publish,
    backend_create_snapshot,
    backend_delete_snapshot,
    backend_list_snapshots,
    backend_cleanup,
    backend_tasks,
    backend_delete_task,
    backend_clear_tasks,
)


jennycli = argh.EntryPoint("Jenny")


def log_action_cli(
    action: str,
    environments: list[str],
    distributions: list[str],
    packages: list[str],
) -> None:
    currentuser = pwd.getpwuid(os.getuid()).pw_name
    return log_action(
        ip=platform.node(),
        user=currentuser,
        action=action,
        environments=environments,
        distributions=distributions,
        packages=packages,
    )


@jennycli
def list_dists() -> None:
    "List known distributions"
    dists = list(jennyconfig["dists"].keys())
    dists.sort(key=str)
    for d in dists:
        print(d)


@jennycli
def list_packages(distribution: str, env: str) -> None:
    "List packages in a distribution"
    dist = de2str(distribution, env)
    # for p in backend_read_packages(dist, sort=True):
    #    print("%s/%s/%s" % (p.name, p.version, p.architecture))
    data = backend_read_packages_grouped(dist)
    for s in data:
        print("Source package %s" % (s,))
        for p in data[s]:
            print("  %s/%s/%s" % (p.name, p.version, p.architecture))


# TODO fix rendering
@jennycli
def compare_dists(leftenv: str, rightenv: str, dists: str, *packages: list[str]):
    dists = dists.split(urlsep)

    if m := re.search("(.*)=(.*)", leftenv):
        leftenv = m.group(1)
        leftsnap = m.group(2)
    else:
        leftsnap = None

    if m := re.search("(.*)=(.*)", rightenv):
        rightenv = m.group(1)
        rightsnap = m.group(2)
    else:
        rightsnap = None

    if packages:
        package_filter = [i.strip() for i in packages if i]
    else:
        package_filter = None

    diffdatas = {
        d: backend_diff_dists_grouped(
            d, leftenv, leftsnap, rightenv, rightsnap, package_filter
        )
        for d in dists
    }
    template = jenv.get_template("compare-dists.txt")
    return template.render(
        diffdatas=diffdatas,
        leftenv=leftenv,
        leftsnap=leftsnap,
        rightenv=rightenv,
        rightsnap=rightsnap,
    )


@jennycli
def update() -> None:
    "Update mirror distributions from their upstream repositories"
    log_action_cli("update", [], [], [])
    backend_update_mirrors()


@jennycli
def include_changes(dist: str, changes: str) -> None:
    "Include a built package (*.changes) into a distribution"
    (environment, distribution) = str2de(dist)
    log_action_cli("include-changes", [environment], [distribution], [changes])
    backend_include_changes(dist, changes)


@jennycli
def include_debs(dist: str, *debs: list[str]) -> None:
    "Include one or multiple built package(s) (*.deb) into a distribution"
    (environment, distribution) = str2de(dist)
    log_action_cli("include-debs", [environment], [distribution], debs)
    backend_include_debs(dist, debs)


@jennycli
def migrate_package(dist: str, fromenv: str, toenv: str, package: str) -> None:
    "Copy a package from one environment to another"
    log_action_cli("migrate-package", [fromenv, toenv], [dist], [package])
    backend_migrate_packages(
        fromenv,
        toenv,
        {dist: [package]},
    )


@jennycli
def migrate_all_packages(fromenv: str, toenv: str, *dists: list[str]) -> None:
    "Copy all packages from one environment to another"
    log_action_cli("migrate-all-packages", [fromenv, toenv], dists, ["all"])
    backend_migrate_all_packages(
        fromenv,
        toenv,
        dists,
    )


@jennycli
def remove_packages(distribution: str, environment: str, package: str) -> None:
    "Remove a package from a distribution"
    log_action_cli("remove-package", [environment], [distribution], [package])
    backend_remove_packages(distribution, environment, [package])


@jennycli
def import_keys() -> None:
    "Import repository signature keys"
    for k in glob.glob("keys/*.asc"):
        subprocess.check_output(
            ["gpg", "--import", k],
            text=True,
            stderr=subprocess.STDOUT,
        )


@jennycli
def init() -> None:
    "Do all one-time steps to configure jenny and its backend"
    import_keys()
    log_action_cli("init", [], [], [])
    backend_init()


@jennycli
def incoming_daemon() -> None:
    "Jenny daemon to process incoming queues"
    backend_incoming_daemon()


@jennycli
def publish(target: str, *dists) -> None:
    "Publish repository set"
    log_action_cli("publish", [], [], [])
    backend_publish(target, dists)


@jennycli
def fill_distribution_from_source(
    distribution: str, environment: str, url: str, suite: str
) -> None:
    "Inject packages from an external source into a distribution"
    log_action_cli("fill-distribution-from-source", [environment], [distribution], [])
    backend_fill_distribution_from_source(distribution, environment, url, suite)


@jennycli
def search(package: str) -> None:
    "Search packages in all distributions"
    pprint(backend_search_package(package))


@jennycli
def create_web_user(email: str, password: str) -> None:
    import webapp

    webapp.create_user(email, password)


@jennycli
def cleanup() -> None:
    backend_drop_old_tmp_snapshots()
    backend_drop_upload_dirs()
    backend_cleanup()


@jennycli
def add_package(
    distribution: str, environment: str, component: str, *package: list[str]
) -> None:
    backend_add_package_from_files(distribution, environment, component, package)


@jennycli
def dump_config() -> None:
    pprint(jennyconfig)


@jennycli
def drop_all_publish() -> None:
    "Drop all published repositories"
    backend_drop_all_publish()


@jennycli
def create_snapshot(environment: str, name: str) -> None:
    backend_create_snapshot(environment, name)


@jennycli
def delete_snapshot(environment: str, name: str) -> None:
    backend_delete_snapshot(environment, name)


@jennycli
def list_snapshots() -> None:
    snapshots = backend_list_snapshots()
    for env in snapshots:
        print(f"{env}:")
        for snap in snapshots[env]:
            print(f"  {snap}: {snapshots[env][snap].strftime('%Y-%m-%d %H:%M:%S')}")


@jennycli
def list_tasks() -> None:
    pprint(backend_tasks())


@jennycli
def delete_task(tid: int) -> None:
    backend_delete_task(tid)


@jennycli
def clear_tasks() -> None:
    backend_clear_tasks()

#! /usr/bin/python3

"""Web frontend for Brian

Provides a UI to:
 * list distributions
 * browse packages available in distributions
 * remove selected packages from distributions
 * compare the contents of distributions across environments
 * migrate packages across environments within a distribution
 * also an "expert migration" allowing to migrate packages across
   distributions and components without the normal workflow
   constraints.
"""

import re
import os
import urllib
from functools import cmp_to_key
import apt_pkg

from flask import Flask, render_template, redirect, url_for, g, request, session
import werkzeug

from base import urlsep, de2str, str2dec, PackageEntry, logger  # , dec2str
from brianlog import log_action
from config import load_config, brianconfig
from backend import (
    brian_basedir,
    backend_diff_dists_grouped,
    backend_read_packages_grouped,
    backend_migrate_packages,
    backend_migrate_packages_expert,
    backend_remove_packages,
    backend_search_package,
    backend_list_snapshots,
    backend_create_snapshot,
    backend_delete_snapshot,
    backend_tasks,
    backend_delete_task,
    backend_clear_tasks,
    backend_task_info,
    snapre,
)


webapp = Flask(__name__)
webapp.secret_key = "theen6OoPheej4iefeeP"
webapp.instance_path = os.path.join(brianconfig["brian"]["basedir"], "flask")

stagere = "[a-zA-Z0-9]*"
envre = "[a-z0-9]*"
distre = "[a-z0-9][-a-z0-9+./]*"
pkgre = "[a-z0-9][-a-z0-9+.]*"


def parse_stage(s):
    if re.search(f"^{stagere}$", s):
        return s
    else:
        return None


def parse_env(s, prefix=""):
    if m := re.search(f"^{prefix}({envre})$", s):
        return m.group(1)
    else:
        return None


def parse_snap(s, prefix=""):
    if m := re.search(f"^{prefix}({snapre})$", s):
        if m.group(1) != "None":
            return m.group(1)
    return None


def parse_env_env(s, prefix="", sep="/"):
    if m := re.search(f"^{prefix}({envre}){sep}({envre})$", s):
        return m.groups()
    else:
        return None


def parse_envsnap_envsnap(s, prefix="", sep="/"):
    if m := re.search(
        f"^{prefix}({envre})(?:=({snapre}))?{sep}({envre})(?:=({snapre}))?$", s
    ):
        (leftenv, leftsnap, rightenv, rightsnap) = m.groups()
        if leftsnap == "None":
            leftsnap = None
        if rightsnap == "None":
            rightsnap = None
        return (leftenv, leftsnap, rightenv, rightsnap)
    else:
        return None


def parse_envsnap(s, prefix=""):
    if m := re.search(f"^{prefix}({envre})(?:=({snapre}))?$", s):
        (env, snap) = m.groups()
        if snap == "None":
            snap = None
        return (env, snap)
    else:
        return None


def parse_dist(s, prefix=""):
    if m := re.search(f"^{prefix}({distre})$", s):
        return m.group(1)
    else:
        return None


def parse_distlist(s):
    dists = [i.strip() for i in s.split(urlsep)]
    dists = [parse_dist(i) for i in dists]
    return [i for i in dists if i]


def parse_dist_pkg(s, prefix="", sep="/"):
    if m := re.search(f"^{prefix}({distre}){sep}({pkgre})$", s):
        return m.groups()
    else:
        return None


def parse_pkg(s, prefix=""):
    if m := re.search(f"^{prefix}({pkgre})$", s):
        return m.group(1)
    else:
        return None


def parse_pkglist(s):
    pkgs = [i.strip() for i in s.split(urlsep)]
    pkgs = [parse_pkg(i) for i in pkgs]
    return [i for i in pkgs if i]


# @webapp.errorhandler(Exception)
def handle_exception(e):
    return render_template("500.html", e=e), 500


@webapp.before_request
def webapp_before_request():
    load_config()
    try:
        g.brianconfig = brianconfig["brian"]
        g.distributions = brianconfig["dists"]
        g.distsperbasename = brianconfig["distsperbasename"]
        g.stages = brianconfig["stages"]
        g.environments = brianconfig["environments"]
        g.envpairs = []
        ed = dict(enumerate(g.environments))
        for i in ed:
            for j in ed:
                if i < j:
                    g.envpairs.append([ed[i], ed[j]])
        g.snapshots = backend_list_snapshots()
        g.session = session
    except RuntimeError:
        pass
    if request.remote_user:
        g.can_write = True
        g.current_user = request.remote_user
    elif "HTTP_REMOTE_USER" in request.environ:
        g.can_write = True
        g.current_user = request.environ["HTTP_REMOTE_USER"]
    elif request.path.startswith("/anon/"):
        g.can_write = False
        g.current_user = None
    else:
        # We rely on Apache authentication
        # If we ever need to differenciate access levels, see state of tree as of
        # commit d6c45f0b72be6aaefce63e62ed2843b9befdefeb for the pure Flask implementation
        raise werkzeug.exceptions.InternalServerError(
            f"No HTTP Auth provided by web server for path {request.path}"
        )
    g.referrer = request.referrer
    g.path = request.path


@webapp.route("/list-dists")
def webapp_list_dists():
    # log_action(
    #     ip=request.remote_addr,
    #     user=g.current_user,
    #     action="list-dists",
    #     environments=[],
    #     distributions=[],
    #     packages=[],
    # )
    return render_template("list-dists.html")


@webapp.route("/list-snapshots")
def webapp_list_snapshots():
    return render_template("list-snapshots.html")


@webapp.route("/create-snapshot", methods=["POST"])
def webapp_create_snapshot():
    env = parse_env(request.form["env"])
    snapname = parse_snap(request.form["snapname"])
    backend_create_snapshot(env, snapname)
    log_action(
        ip=request.remote_addr,
        user=g.current_user,
        action="create-snapshot",
        environments=[env],
        distributions=[],
        packages=[],
    )
    return redirect("/list-snapshots")


@webapp.route("/delete-snapshot", methods=["POST"])
def webapp_delete_snapshot():
    env = parse_env(request.form["env"])
    snapname = parse_snap(request.form["snapname"])
    backend_delete_snapshot(env, snapname)
    log_action(
        ip=request.remote_addr,
        user=g.current_user,
        action="delete-snapshot",
        environments=[env],
        distributions=[],
        packages=[],
    )
    return redirect("/list-snapshots")


@webapp.route("/set-stage/<stage>")
def webapp_set_stage(stage: str):
    stage = parse_stage(stage)
    if stage in brianconfig["stages"]:
        session["curstage"] = stage
    else:
        session["curstage"] = ""
    return redirect("/list-dists")


@webapp.route("/dist-action", methods=["POST"])
def webapp_dist_action():
    try:
        dists = session["dists"]
    except KeyError:
        dists = []
    try:
        leftenv = session["leftenv"]
        rightenv = session["rightenv"]
    except KeyError:
        leftenv = "test"
        rightenv = "stable"
    try:
        curenv = session["curenv"]
    except KeyError:
        curenv = leftenv

    if not request.form:
        return redirect(url_for("webapp_list_dists"))

    if "packages" in request.form:
        pkglist = parse_pkglist(request.form["packages"])

        packages = urlsep.join(pkglist)
    else:
        packages = None
    if "list" in request.form and "env" in request.form:
        (curenv, cursnap) = parse_envsnap(request.form["env"])
        dists = []
        for b in request.form:
            if d := parse_dist(b, prefix="comparedist-"):
                dists.append(urllib.parse.quote(d, safe=""))
        if curenv not in [leftenv, rightenv]:
            leftenv = curenv
        session["curenv"] = curenv
        session["leftenv"] = leftenv
        curenvsnap = curenv
        if cursnap:
            curenvsnap += f"={cursnap}"
        if packages:
            u = url_for(
                "webapp_list_packages",
                env=curenvsnap,
                dists=urlsep.join(dists),
                packages=packages,
            )
            return redirect(u)
        else:
            u = url_for(
                "webapp_list_packages",
                env=curenvsnap,
                dists=urlsep.join(dists),
            )
            return redirect(u)
    elif (
        "compare" in request.form
        and "leftenv" in request.form
        and "leftenv" in request.form
    ):
        (leftenv, leftsnap) = parse_envsnap(request.form["leftenv"])
        (rightenv, rightsnap) = parse_envsnap(request.form["rightenv"])
        dists = []
        for b in request.form:
            if d := parse_dist(b, prefix="comparedist-"):
                dists.append(urllib.parse.quote(d, safe=""))
        session["leftenv"] = leftenv
        session["rightenv"] = rightenv
        if leftsnap:
            lenv = f"{leftenv}={leftsnap}"
        else:
            lenv = leftenv
        if rightsnap:
            renv = f"{rightenv}={rightsnap}"
        else:
            renv = rightenv
        if packages:
            u = url_for(
                "webapp_compare_dists",
                leftenv=lenv,
                rightenv=renv,
                dists=urlsep.join(dists),
                packages=packages,
            )
            return redirect(u)
        else:
            u = url_for(
                "webapp_compare_dists",
                leftenv=lenv,
                rightenv=renv,
                dists=urlsep.join(dists),
            )
            return redirect(u)
    return redirect(url_for("webapp_list_dists"))


@webapp.route("/list-packages/<env>/<dists>")
@webapp.route("/list-packages/<env>/<dists>/<packages>")
def webapp_list_packages(env: str, dists: str, packages: str = None):
    if m := re.search("(.*)=(.*)", env):
        env = m.group(1)
        snap = m.group(2)
    else:
        snap = None
    curenv = env
    cursnap = snap
    dists = dists.split(urlsep)
    dists = [urllib.parse.unquote(d) for d in dists]
    curdist = dists[0]
    session["curdist"] = curdist
    session["curenv"] = curenv
    session["cursnap"] = cursnap
    session["dists"] = dists
    session["packages"] = packages if packages else ""
    plist = packages.split(sep=urlsep) if packages else []
    if packages:
        q = "|".join(
            [f"((Name (= {package}))|($Source (= {package})))" for package in plist]
        )
    else:
        q = None
    # log_action(
    #     ip=request.remote_addr,
    #     user=g.current_user,
    #     action="list-packages",
    #     environments=[env],
    #     distributions=dists,
    #     packages=plist,
    # )
    return render_template(
        "list-packages.html",
        distribution=curdist,
        environment=curenv,
        snap=cursnap,
        packages=packages if packages else "",
        listperdist={
            d: backend_read_packages_grouped(de2str(d, curenv), snap=snap, q=q)
            for d in dists
        },
    )


@webapp.route("/compare-dists/<leftenv>/<rightenv>/<dists>")
@webapp.route("/compare-dists/<leftenv>/<rightenv>/<dists>/<packages>")
def webapp_compare_dists(leftenv: str, rightenv: str, dists: str, packages: str = None):
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

    dists = dists.split(urlsep)
    dists = [urllib.parse.unquote(d) for d in dists]
    session["dists"] = dists
    session["leftenv"] = leftenv
    session["rightenv"] = rightenv
    session["leftsnap"] = leftsnap
    session["rightsnap"] = rightsnap
    session["packages"] = packages if packages else ""
    if packages:
        package_filter = [i.strip() for i in packages.split(urlsep) if i]
    else:
        package_filter = None

    diffdatas = {
        d: backend_diff_dists_grouped(
            d, leftenv, leftsnap, rightenv, rightsnap, package_filter
        )
        for d in dists
    }
    # log_action(
    #     ip=request.remote_addr,
    #     user=g.current_user,
    #     action="compare-dists",
    #     environments=[leftenv, rightenv],
    #     distributions=dists,
    #     packages=package_filter if package_filter else "-",
    # )
    return render_template(
        "compare-dists.html",
        diffdatas=diffdatas,
        leftenv=leftenv,
        leftsnap=leftsnap,
        rightenv=rightenv,
        rightsnap=rightsnap,
        packages=packages if packages else "",
    )


def migratediff():
    leftenv = parse_env(request.form["leftenv"])
    rightenv = parse_env(request.form["rightenv"])
    leftsnap = parse_snap(request.form["leftsnap"])
    rightsnap = parse_snap(request.form["rightsnap"])
    if "fromenv" in request.form:
        fromenv = parse_env(request.form["fromenv"])
        toenv = parse_env(request.form["toenv"])
        fromsnap = parse_snap(request.form["fromsnap"])
    else:
        if "migrate-left" in request.form:
            fromenv = rightenv
            toenv = leftenv
            fromsnap = rightsnap
        else:
            fromenv = leftenv
            toenv = rightenv
            fromsnap = leftsnap
    fromenvsnap = fromenv
    if fromsnap:
        fromenvsnap += f"[{fromsnap}]"

    srcpkgs = []
    dists = []
    srcpkgsperdist = {}
    addedversions = 0
    removedversions = 0
    migratedpackages = 0
    for p in request.form:
        if x := parse_dist_pkg(p, prefix="migratesrcpkg/"):
            (dist, pkg) = x
            if dist not in srcpkgsperdist:
                srcpkgsperdist[dist] = set()
            srcpkgsperdist[dist].add(pkg)
        if d := parse_dist(p, prefix="migratedist/"):
            dists.append(urllib.parse.quote(d, safe=""))
    sorteddiffs = {}
    for dist in srcpkgsperdist:
        diffs = {}
        q = "|".join(
            [
                f"(Name (= {package}))|($Source (= {package}))"
                for package in srcpkgsperdist[dist]
            ]
        )

        d = backend_read_packages_grouped(de2str(dist, fromenv), q=q)

        diffdata = backend_diff_dists_grouped(
            dist, fromenv, fromsnap, toenv, None, srcpkgsperdist[dist]
        )

        for p in diffdata["diffs"]:
            diffs[p] = set()
            for a in diffdata["diffs"][p]:
                if "left" in diffdata["diffs"][p][a]:
                    for v in diffdata["diffs"][p][a]["left"]:
                        pp = PackageEntry(
                            p, v, dist, diffdata["diffs"][p][a]["component"], a
                        )
                        diffs[p].add((pp, "added"))
                        addedversions += 1
                if "right" in diffdata["diffs"][p][a]:
                    for v in diffdata["diffs"][p][a]["right"]:
                        pp = PackageEntry(
                            p, v, dist, diffdata["diffs"][p][a]["component"], a
                        )
                        diffs[p].add((pp, "removed"))
                        removedversions += 1

        sorteddiffs[dist] = {}
        for p in diffs:
            sorteddiffs[dist][p] = sorted(
                list(diffs[p]),
                key=cmp_to_key(
                    lambda x, y: apt_pkg.version_compare(x[0].version, y[0].version)
                ),
            )
            migratedpackages += 1

        for p in d:
            pdata = [i for i in d[p] if i.architecture == "source"]
            if pdata:
                for pd in pdata:
                    srcpkgs.append((dist, pd))
                continue
            pdata = [i for i in d[p] if i.name in srcpkgsperdist[dist]]
            if pdata:
                for pd in pdata:
                    srcpkgs.append((dist, pd))
                continue
            pdata = d[p]
            if pdata:
                for pd in pdata:
                    srcpkgs.append((dist, pd))
                continue

    session["leftenv"] = leftenv
    session["rightenv"] = rightenv
    session["leftsnap"] = leftsnap
    session["rightsnap"] = rightsnap

    return (
        leftenv,
        rightenv,
        leftsnap,
        rightsnap,
        fromenv,
        toenv,
        fromsnap,
        fromenvsnap,
        srcpkgs,
        dists,
        srcpkgsperdist,
        addedversions,
        removedversions,
        migratedpackages,
        sorteddiffs,
    )


@webapp.route("/pre-migrate-packages", methods=["POST"])
def webapp_pre_migrate_packages():
    (
        leftenv,
        rightenv,
        leftsnap,
        rightsnap,
        fromenv,
        toenv,
        fromsnap,
        _,  # fromenvsnap
        srcpkgs,
        dists,
        srcpkgsperdist,
        addedversions,
        removedversions,
        migratedpackages,
        sorteddiffs,
    ) = migratediff()

    return render_template(
        "pre-migrate-packages.html",
        leftenv=leftenv,
        rightenv=rightenv,
        fromenv=fromenv,
        toenv=toenv,
        leftsnap=leftsnap,
        rightsnap=rightsnap,
        fromsnap=fromsnap,
        srcpackages=srcpkgs,
        addedversions=addedversions,
        removedversions=removedversions,
        migratedpackages=migratedpackages,
        srcpkgsperdist=srcpkgsperdist,
        dists=dists,
        diffs=sorteddiffs,
    )


@webapp.route("/post-list-packages", methods=["POST"])
def webapp_post_list_packages():
    if "remove" in request.form:
        return webapp_pre_remove_packages()
    elif "expert-migrate" in request.form:
        return webapp_expert_migrate()
    else:
        return redirect(
            url_for(
                "webapp_list_packages",
                dists=",".join(session["dists"]),
                env=parse_env(request.form["environment"]),
            )
        )


def webapp_expert_migrate():
    redirurl = url_for(
        "webapp_list_packages",
        dists=",".join(session["dists"]),
        env=parse_env(request.form["environment"]),
    )

    srcpkgs = []
    env = parse_env(request.form["environment"])
    print(request.form)
    if "expert-sure" not in request.form:
        return redirect(redirurl)
    for p in request.form:
        if x := parse_dist_pkg(p, prefix="srcpkg/"):
            srcpkgs.append(x)
    dists = {}
    for s in srcpkgs:
        if s[0] not in dists:
            dists[s[0]] = []
        dists[s[0]].append(s[1])
    targetdist, targetenv, targetcomp = str2dec(request.form["targetrepo"])
    for d in dists:
        print(d)
        log_action(
            ip=request.remote_addr,
            user=g.current_user,
            action="expert-migrate",
            environments=[targetenv],
            distributions=[targetdist],
            packages=dists[d],
        )
        backend_migrate_packages_expert(
            fromdist=d,
            fromenv=env,
            todist=targetdist,
            toenv=targetenv,
            tocomp=targetcomp,
            pkgs=dists[d],
            asyncpub=True,
        )
    return redirect(redirurl)


def removediff():
    srcpkgs = []
    dists = []
    env = parse_env(request.form["environment"])
    srcperdist = {}
    for p in request.form:
        if x := parse_dist_pkg(p, prefix="srcpkg/"):
            if (dist := x[0]) not in srcperdist:
                srcperdist[dist] = set()
            srcperdist[dist].add(x[1])
            srcpkgs.append(x)
        if d := parse_dist(p, prefix="removedist/"):
            dists.append(urllib.parse.quote(d, safe=""))

    removed = {}
    removedversions = 0
    for d in srcperdist:
        removed[d] = {}
        q = "|".join(
            [
                f"(Name (= {package}),$PackageType (= source))|($Source (= {package}))"
                for package in srcperdist[d]
            ]
        )

        res = backend_read_packages_grouped(de2str(d, env), q=q)
        for p in res:
            removed[d][p] = sorted(
                res[p],
                key=cmp_to_key(
                    lambda x, y: apt_pkg.version_compare(x.version, y.version)
                ),
            )
            removedversions += len(removed[d][p])

    return srcpkgs, dists, env, srcperdist, removed, removedversions


def webapp_pre_remove_packages():
    (srcpkgs, dists, env, _, removed, removedversions) = removediff()
    return render_template(
        "pre-remove-packages.html",
        environment=env,
        srcpackages=srcpkgs,
        removed=removed,
        removedpackages=len(srcpkgs),
        removedversions=removedversions,
        dists=dists,
    )


@webapp.route("/migrate-packages", methods=["POST"])
def webapp_migrate_packages():
    (
        leftenv,
        rightenv,
        leftsnap,
        rightsnap,
        fromenv,
        toenv,
        fromsnap,
        fromenvsnap,
        _,  # srcpkgs
        _,  # dists
        srcpkgsperdist,
        addedversions,
        removedversions,
        migratedpackages,
        sorteddiffs,
    ) = migratediff()

    migrated = backend_migrate_packages(
        fromenv=fromenv,
        fromsnap=fromsnap,
        toenv=toenv,
        srcpkgs=srcpkgsperdist,
        asyncpub=True,
    )

    dists = urlsep.join([urllib.parse.quote(d, safe="") for d in session["dists"]])

    leftenvsnap = leftenv
    if leftsnap:
        leftenvsnap += f"={leftsnap}"
    rightenvsnap = rightenv
    if rightsnap:
        rightenvsnap += f"={rightsnap}"
    if session["packages"]:
        redir = url_for(
            "webapp_compare_dists",
            leftenv=leftenvsnap,
            rightenv=rightenvsnap,
            dists=dists,
            packages=session["packages"],
        )
    else:
        redir = url_for(
            "webapp_compare_dists",
            leftenv=leftenvsnap,
            rightenv=rightenvsnap,
            dists=dists,
        )
    packlist = []
    for d in sorteddiffs:
        for p in sorteddiffs[d]:
            packlist.append(f"{d}/{p}")
    log_action(
        ip=request.remote_addr,
        user=g.current_user,
        action="migrate-packages",
        environments=[fromenv, toenv],
        distributions=session["dists"],
        packages=packlist,
    )
    return render_template(
        "post-migrate-packages.html",
        fromenvsnap=fromenvsnap,
        toenv=toenv,
        migrated=migrated,
        redir=redir,
        diffs=sorteddiffs,
        addedversions=addedversions,
        removedversions=removedversions,
        migratedpackages=migratedpackages,
    )


@webapp.route("/remove-packages", methods=["POST"])
def webapp_remove_packages():
    (srcpkgs, dists, env, srcperdist, removed, removedversions) = removediff()

    for d in srcperdist:
        backend_remove_packages(
            distribution=d,
            environment=env,
            packages=srcperdist[d],
            asyncpub=True,
        )
    dists = urlsep.join([urllib.parse.quote(d, safe="") for d in session["dists"]])
    if session["packages"]:
        redir = url_for(
            "webapp_list_packages",
            env=env,
            dists=dists,
            packages=session["packages"],
        )
    else:
        redir = url_for(
            "webapp_list_packages",
            env=env,
            dists=dists,
        )
    log_action(
        ip=request.remote_addr,
        user=g.current_user,
        action="remove-packages",
        environments=[env],
        distributions=session["dists"],
        packages=[f"{p[0]}/{p[1]}" for p in srcpkgs],
    )
    return render_template(
        "post-remove-packages.html",
        environment=env,
        srcpackages=srcpkgs,
        removed=removed,
        removedpackages=len(srcpkgs),
        removedversions=removedversions,
        dists=dists,
        redir=redir,
    )


@webapp.route("/anon/search-package/<package>/<version>")
def webapp_search_package(package: str, version: str):
    plist = backend_search_package(package)
    for k in plist:
        for pe in plist[k]:
            if version == pe.version:
                (d, e, c) = k
                return f"FOUND in dist {d} environment {e} component {c}"
    return "NOTFOUND"


@webapp.route("/api/upload-package/<dist>", methods=["POST"])
def webapp_upload_package(dist: str):
    incomingdir = os.path.join(
        brian_basedir, "incoming", f"{dist}--{brianconfig['environments'][0]}"
    )

    if request.method != "POST":
        return

    files = request.files.getlist("files")
    for f in files:
        destfile = os.path.join(incomingdir, f.filename)
        f.save(destfile)
    dist = urllib.parse.unquote(dist)
    log_action(
        ip=request.remote_addr,
        user=g.current_user,
        action="upload-package",
        environments=[brianconfig["environments"][0]],
        distributions=[dist],
        packages=[f.filename for f in files],
    )
    return "OK"


@webapp.route("/list-tasks")
def webapp_list_tasks():
    return render_template(
        "list-tasks.html",
        tasks=backend_tasks(),
    )


@webapp.route("/remove-task/<tid>")
def webapp_ack_task(tid: int):
    backend_delete_task(tid)
    return redirect(url_for("webapp_list_tasks"))


@webapp.route("/task-details/<tid>")
def webapp_task_details(tid: int):
    info = backend_task_info(tid)
    return render_template(
        "task-details.html",
        tid=tid,
        info=info,
    )


@webapp.route("/clear-tasks")
def webapp_clear_tasks():
    backend_clear_tasks()
    return redirect(url_for("webapp_list_tasks"))


@webapp.route("/")
def root():
    return redirect(url_for("webapp_list_dists"))


# Generate a nice key using secrets.token_urlsafe()
webapp.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY", "pf9Wkove4IKEAXvy-cQkeDPhv9Cb3Ag-wyJILbq_dFw"
)
# Generate a good salt for password hashing using: secrets.SystemRandom().getrandbits(128)
webapp.config["SECURITY_PASSWORD_SALT"] = os.environ.get(
    "SECURITY_PASSWORD_SALT", "146585145368132386173505678016728509634"
)

# have session and remember cookie be samesite (flask/flask_login)
webapp.config["REMEMBER_COOKIE_SAMESITE"] = "strict"
webapp.config["SESSION_COOKIE_SAMESITE"] = "strict"

logger.warning("Starting webapp")

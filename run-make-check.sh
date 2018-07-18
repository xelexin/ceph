#!/usr/bin/env bash
#
# Ceph distributed storage system
#
# Copyright (C) 2014 Red Hat <contact@redhat.com>
#
# Author: Loic Dachary <loic@dachary.org>
#
#  This library is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2.1 of the License, or (at your option) any later version.
#

#
# To just look at what this script will do, run it like this:
#
# $ DRY_RUN=echo ./run-make-check.sh
#

set -e

trap clean_up_after_myself EXIT

ORIGINAL_CCACHE_CONF="$HOME/.ccache/ccache.conf"
SAVED_CCACHE_CONF="$HOME/.run-make-check-saved-ccache-conf"

function save_ccache_conf() {
    test -f $ORIGINAL_CCACHE_CONF && cp $ORIGINAL_CCACHE_CONF $SAVED_CCACHE_CONF || true
}

function restore_ccache_conf() {
    test -f $SAVED_CCACHE_CONF && mv $SAVED_CCACHE_CONF $ORIGINAL_CCACHE_CONF || true
}

function clean_up_after_myself() {
    rm -fr ${CEPH_BUILD_VIRTUALENV:-/tmp}/*virtualenv*
    restore_ccache_conf
}

function get_processors() {
    if test -n "$NPROC" ; then
        echo $NPROC
    else
        if test $(nproc) -ge 2 ; then
            expr $(nproc) / 2
        else
            echo 1
        fi
    fi
}

function run() {
    local install_cmd
    local which_pkg="which"
    source /etc/os-release
    if test -f /etc/redhat-release ; then
        if ! type bc > /dev/null 2>&1 ; then
            echo "Please install bc and re-run." 
            exit 1
        fi
        if test "$(echo "$VERSION_ID >= 22" | bc)" -ne 0; then
            install_cmd="dnf -y install"
        else
            install_cmd="yum install -y"
        fi
    elif type zypper > /dev/null 2>&1 ; then
        install_cmd="zypper --gpg-auto-import-keys --non-interactive install --no-recommends"
    elif type apt-get > /dev/null 2>&1 ; then
        install_cmd="apt-get install -y"
        which_pkg="debianutils"
    fi

    if ! type sudo > /dev/null 2>&1 ; then
        echo "Please install sudo and re-run. This script assumes it is running"
        echo "as a normal user with the ability to run commands as root via sudo." 
        exit 1
    fi
    if [ -n "$install_cmd" ]; then
        $DRY_RUN sudo $install_cmd ccache $which_pkg
    else
        echo "WARNING: Don't know how to install packages" >&2
        echo "This probably means distribution $ID is not supported by run-make-check.sh" >&2
    fi

    if ! type ccache > /dev/null 2>&1 ; then
        echo "ERROR: ccache could not be installed"
        exit 1
    fi

    if test -f ./install-deps.sh ; then
	    export WITH_SEASTAR=1
	    $DRY_RUN source ./install-deps.sh || return 1
        trap clean_up_after_myself EXIT
    fi

    # Init defaults after deps are installed. get_processors() depends on coreutils nproc.
    DEFAULT_MAKEOPTS=${DEFAULT_MAKEOPTS:--j$(get_processors)}
    BUILD_MAKEOPTS=${BUILD_MAKEOPTS:-$DEFAULT_MAKEOPTS}
    test "$BUILD_MAKEOPTS" && echo "make will run with option(s) $BUILD_MAKEOPTS"
    CHECK_MAKEOPTS=${CHECK_MAKEOPTS:-$DEFAULT_MAKEOPTS}

    if type python2 > /dev/null 2>&1 ; then
        # gtest-parallel requires Python 2
        CMAKE_PYTHON_OPTS="-DWITH_GTEST_PARALLEL=ON"
    else
        CMAKE_PYTHON_OPTS="-DWITH_PYTHON2=OFF -DWITH_PYTHON3=ON -DMGR_PYTHON_VERSION=3 -DWITH_GTEST_PARALLEL=OFF"
    fi

    CMAKE_BUILD_OPTS="-DWITH_FIO=ON -DWITH_SEASTAR=ON"

    cat <<EOM
Note that the binaries produced by this script do not contain correct time
and git version information, which may make them unsuitable for debugging
and production use.
EOM
    save_ccache_conf
    # remove the entropy generated by the date/time embedded in the build
    CMAKE_BUILD_OPTS="$CMAKE_BUILD_OPTS -DENABLE_GIT_VERSION=OFF"
    $DRY_RUN export SOURCE_DATE_EPOCH="946684800"
    $DRY_RUN ccache -o sloppiness=time_macros
    $DRY_RUN ccache -o run_second_cpp=true
    if [ -n "$JENKINS_HOME" ]; then
        # Build host has plenty of space available, let's use it to keep
        # various versions of the built objects. This could increase the cache hit
        # if the same or similar PRs are running several times
        $DRY_RUN ccache -o max_size=100G
    else
        echo "Current ccache max_size setting:"
        ccache -p | grep max_size
    fi
    $DRY_RUN ccache -z # Reset the ccache statistics

    $DRY_RUN ./do_cmake.sh $CMAKE_BUILD_OPTS $CMAKE_PYTHON_OPTS $@ || return 1
    $DRY_RUN cd build
    $DRY_RUN make $BUILD_MAKEOPTS tests || return 1

    $DRY_RUN ccache -s # print the ccache statistics to evaluate the efficiency

    # to prevent OSD EMFILE death on tests, make sure ulimit >= 1024
    $DRY_RUN ulimit -n $(ulimit -Hn)
    if [ $(ulimit -n) -lt 1024 ];then
        echo "***ulimit -n too small, better bigger than 1024 for test***"
        return 1
    fi
 
    if ! $DRY_RUN ctest $CHECK_MAKEOPTS --output-on-failure; then
        rm -fr ${TMPDIR:-/tmp}/ceph-asok.*
        return 1
    fi
}

function main() {
    if [[ $EUID -eq 0 ]] ; then
        echo "For best results, run this script as a normal user configured"
        echo "with the ability to run commands as root via sudo."
    fi
    echo -n "Checking hostname sanity... "
    if $DRY_RUN hostname --fqdn >/dev/null 2>&1 ; then
        echo "OK"
    else
        echo "NOT OK"
        echo "Please fix 'hostname --fqdn', otherwise 'make check' will fail"
        return 1
    fi
    run "$@" && echo "make check: successful run on $(git rev-parse HEAD)"
}

main "$@"

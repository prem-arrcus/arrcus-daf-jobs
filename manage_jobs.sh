#!/usr/bin/env bash
# Manage Jenkins jobs for the arrcus DAF project.
#
# Validates everything, generates the Jenkins Job Builder (JJB) job
# definitions (jenkins-job-definitions.generated.yaml) from jobs.yaml and
# job_templates.yaml, and pushes (creates / updates) the jobs on the
# Jenkins server configured in jenkins.conf (or the file passed with
# --conf).
#
# The virtual env .env under this directory is used for the jenkins-job
# builder tool; it is created with 'uv venv .env' and populated with
# 'uv pip install jenkins-job-builder' if it does not exist.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "${SCRIPT_DIR}"

CONF="${SCRIPT_DIR}/jenkins.conf"
JOBS_YAML="${SCRIPT_DIR}/jobs.yaml"
NODES_YAML="${SCRIPT_DIR}/nodes.yaml"
TEMPLATES_YAML="${SCRIPT_DIR}/job_templates.yaml"
OUTPUT_YAML="${SCRIPT_DIR}/jenkins-job-definitions.generated.yaml"
VENV="${SCRIPT_DIR}/.env"
PY="${VENV}/bin/python"
JJB="${VENV}/bin/jenkins-jobs"
GENERATOR="${SCRIPT_DIR}/generate_job_definitions.py"

JJB_DEBUG_ARGS=()

usage() {
    cat <<EOF
Usage: ${0##*/} [options] [command]

Manage the DAF Jenkins jobs (create / update / delete) using
jenkins-job-builder.

Commands:
  push (default)    Generate the job definitions and create / update them
                    on the Jenkins server configured in the conf file.
  test              Validate the generated job definitions without
                    contacting the Jenkins server.
  delete            Delete the jobs listed in the generated definitions
                    from the Jenkins server.
  push-nodes        Push the executor node info from nodes.yaml to the
                    Jenkins server (create / update the nodes).
  generate          Only (re-)generate jenkins-job-definitions.generated.yaml.
  setup-env         (Re-)create the .env virtual env and install
                    jenkins-job-builder into it.
  recreate-env      Delete and re-create the .env virtual env.
  shell             Start an interactive shell with .env activated.

Options:
  -c|--conf FILE        Jenkins server config file
                        (default: ${CONF}). Must be an INI file with
                        [job_builder] and [jenkins] sections; see
                        jenkins.sample.conf.
  -j|--jobs FILE        Jobs definition file (default: ${JOBS_YAML})
  -t|--templates FILE   Job templates file (default: ${TEMPLATES_YAML})
  -o|--output FILE      Generated output file
                        (default: ${OUTPUT_YAML})
  -n|--nodes FILE       Node definitions file (default: ${NODES_YAML})
  -v|--verbose          Enable debug logging for jenkins-job-builder.
  -h|--help             Show this help.
EOF
}

die() { echo "${0##*/}: error: $*" >&2; exit 1; }

note() { echo "${0##*/}: $*"; }

# ---------------------------------------------------------------- options --
COMMAND="push"
while [ $# -gt 0 ]; do
    case "$1" in
        -c|--conf)       [ $# -ge 2 ] || die "$1 requires an argument"; CONF="$2"; shift 2 ;;
        -j|--jobs)       [ $# -ge 2 ] || die "$1 requires an argument"; JOBS_YAML="$2"; shift 2 ;;
        -n|--nodes)      [ $# -ge 2 ] || die "$1 requires an argument"; NODES_YAML="$2"; shift 2 ;;
        -t|--templates)  [ $# -ge 2 ] || die "$1 requires an argument"; TEMPLATES_YAML="$2"; shift 2 ;;
        -o|--output)     [ $# -ge 2 ] || die "$1 requires an argument"; OUTPUT_YAML="$2"; shift 2 ;;
        -v|--verbose)    JJB_DEBUG_ARGS=(--log-level DEBUG); shift ;;
        -h|--help)       usage; exit 0 ;;
        push|update)     COMMAND="push"; shift ;;
        push-nodes)      COMMAND="push-nodes"; shift ;;
        test|validate)   COMMAND="test"; shift ;;
        delete|remove)   COMMAND="delete"; shift ;;
        generate)        COMMAND="generate"; shift ;;
        setup-env)       COMMAND="setup-env"; shift ;;
        recreate-env)    COMMAND="recreate-env"; shift ;;
        shell)           COMMAND="shell"; shift ;;
        *)               die "unknown option or command: $1 (see --help)" ;;
    esac
done

# ------------------------------------------------------------- virtual env --
ensure_env() {
    if [ ! -x "${PY}" ]; then
        command -v uv >/dev/null 2>&1 \
            || die "uv is required to create the virtual env (.env) but was not found"
        note "virtual env not found - creating one at .env using 'uv venv .env'"
        uv venv "${VENV}"
    fi
    # setuptools<81 is required on newer Python versions: jenkins-job-builder
    # loads its plugins through stevedore/pkg_resources, which was removed
    # from setuptools >= 81.
    note "installing jenkins-job-builder into the virtual env (.env)"
    uv pip install --python "${PY}" -q jenkins-job-builder 'setuptools<81'
}

# --------------------------------------------------------------- generate --
generate_definitions() {
    note "generating job definitions: ${JOBS_YAML} + ${TEMPLATES_YAML} -> ${OUTPUT_YAML}"
    "${PY}" "${GENERATOR}" --jobs "${JOBS_YAML}" --templates "${TEMPLATES_YAML}" \
        --output "${OUTPUT_YAML}"
}

names_from_output() {
    "${PY}" "${GENERATOR}" --output "${OUTPUT_YAML}" --list-names
}

# ------------------------------------------------------------ jenkins conf --
require_conf() {
    if [ ! -f "${CONF}" ]; then
        die "jenkins conf file not found: ${CONF}
  ${0##*/} uses the conf file to talk to your Jenkins server.
  Either pass one with --conf <file>, or create ${CONF} based on
  ${SCRIPT_DIR}/jenkins.sample.conf and edit it with your server
  details ([job_builder] and [jenkins] sections)."
    fi
}

exec_jjb() {
    JJB_CMDLINEINFO=()
    if [ -f "${CONF}" ]; then
        JJB_CMDLINEINFO=(--conf "${CONF}")
    fi
    "${JJB}" "${JJB_DEBUG_ARGS[@]}" "${JJB_CMDLINEINFO[@]}" "$@"
}

# ----------------------------------------------------------------- driver --
case "${COMMAND}" in
    setup-env)
        ensure_env
        note "virtual env ready: ${VENV}"
        ;;

    recreate-env)
        note "re-creating the virtual env (.env)"
        rm -rf "${VENV}"
        ensure_env
        note "virtual env ready: ${VENV}"
        ;;

    generate)
        ensure_env
        generate_definitions
        ;;

    shell)
        if [ ! -x "${PY}" ]; then
            note "virtual env not found yet - setting it up first"
            ensure_env
        fi
        note "spawning a shell with the virtual env (${VENV}) activated"
        exec sh -c ". '${VENV}/bin/activate'; exec bash -i"
        ;;

    test)
        ensure_env
        generate_definitions
        note "testing generated job definitions with jenkins-job-builder"
        # test does not contact the Jenkins server, so the conf file is
        # optional (default JJB job_builder settings are used if the conf
        # file is missing).
        if [ -f "${CONF}" ]; then
            exec_jjb test "${OUTPUT_YAML}" >/dev/null
        else
            note "conf file not found (${CONF}); using jenkins-job-builder defaults for the dry run"
            "${JJB}" "${JJB_DEBUG_ARGS[@]}" test "${OUTPUT_YAML}" >/dev/null
        fi
        note "validation successful: ${OUTPUT_YAML}"
        ;;

    delete)
        ensure_env
        require_conf
        if [ ! -f "${OUTPUT_YAML}" ]; then
            generate_definitions
        fi
        mapfile -t NAMES < <(names_from_output)
        [ "${#NAMES[@]}" -gt 0 ] || die "no jobs found in ${OUTPUT_YAML}"
        note "deleting job(s) from the Jenkins server: ${NAMES[*]}"
        exec_jjb delete "${NAMES[@]}"
        note "deleted job(s): ${NAMES[*]}"
        ;;

    push)
        ensure_env
        require_conf
        generate_definitions
        note "validating generated job definitions with jenkins-job-builder"
        if [ -f "${CONF}" ]; then
            exec_jjb test "${OUTPUT_YAML}" >/dev/null
        else
            "${JJB}" "${JJB_DEBUG_ARGS[@]}" test "${OUTPUT_YAML}" >/dev/null
        fi
        note "pushing job(s) to the Jenkins server (${CONF})"
        exec_jjb update "${OUTPUT_YAML}"
        note "done - pushed job(s) from ${OUTPUT_YAML}"
        ;;

    push-nodes)
        ensure_env
        require_conf
        note "pushing executor node info from ${NODES_YAML}"
        "${PY}" "${SCRIPT_DIR}/push_nodes.py" --conf "${CONF}" --nodes "${NODES_YAML}"
        note "done - pushed executor node info to the Jenkins server"
        ;;
esac

#!/usr/bin/env python3
"""Generate jenkins-job-definitions.generated.yaml.

Reads the per-job information from jobs.yaml, combines it with the common
job template(s) from job_templates.yaml, and writes the resulting Jenkins
Job Builder (JJB) job definitions to jenkins-job-definitions.generated.yaml.

If jobs.yaml does not exist, a sample jobs.yaml containing a single job is
generated first.
"""

import copy
import os
import re
import sys
from typing import NoReturn

import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_JOBS_YAML = os.path.join(SCRIPT_DIR, "jobs.yaml")
DEFAULT_TEMPLATES_YAML = os.path.join(SCRIPT_DIR, "job_templates.yaml")
DEFAULT_OUTPUT_YAML = os.path.join(
    SCRIPT_DIR, "jenkins-job-definitions.generated.yaml")

TEMPLATE_KEY = "daf_test_job"
PIPELINE_TEMPLATE_KEY = "daf_pipeline_job"

DEFAULT_DAF_BRANCH = "main"
DEFAULT_POST_SCRIPT = ""

GIT_REPOS_KEY = "git_repos"
GIT_MIRROR_DIR_KEY = "git_mirror_dir"
DEFAULT_GIT_MIRROR_DIR = "/var/cache/git"

REQUIRED_JOB_FIELDS = ("name", "report_email")
UT_KNOWN_JOB_FIELDS = (
    "name", "daf_branch", "yang_branch", "report_email", "script",
    "post_script", "auth_token", "extra_args", "topo_file", "vpp", "label")
UT_KNOWN_GIT_REPO_FIELDS = ("url", "name", "credentials_id")

# Placeholders {name}, {daf_branch}, ... in the templates must not be
# confused with the Jenkins-style parameter references ${daf_branch} that
# also appear literally in the templates and must survive generation
# untouched.
PLACEHOLDER_RE = re.compile(r"(?<!\$)\{(\w+)\}")

JOBS_SAMPLE = """\
# List of Jenkins jobs, one entry per job, with information that is
# unique to each job.
#
# Required fields:
#   name          - name of the Jenkins job
#   report_email  - email address of the user to whom the report is sent
#
# Optional fields:
#   daf_branch    - git branch of the arrcus-daf repo (default: master)
#   yang_branch   - yang models branch; when set, --yang-branch
#                   <yang_branch> is added to the pytest command line
#                   (optional; defaults to the pytest/daf default)
#   script        - pytest script to run; if not specified, the job
#                   defaults to daf/tests/<job name>/run_tests.py
#   post_script   - post-processing script to run after the test script
#   extra_args    - default extra arguments appended to the pytest
#                   command line (optional; the DAF Pipeline always
#                   overrides it with the pipeline's own extra_args)
#   topo_file     - default topology file (--topo-file for pytest);
#                   (optional; defaults to daf/tests/<script>/topo.yaml)
#   vpp           - default state of the vpp build parameter (boolean,
#                   optional; adds --vpp to the pytest command line when
#                   enabled; inherited by the DAF Pipeline when its own
#                   vpp parameter is enabled)
#   auth_token    - token to trigger the job remotely
#                   (defaults to the job name)
#   label         - node label expression that restricts where the job
#                   can run (optional; defaults to "daf")

jobs:
  - name: daf-ace-regression
    daf_branch: master
    report_email: premkumar@arrcus.com
"""


def die(msg) -> NoReturn:
    print(f"generate_job_definitions.py: error: {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg):
    print(f"generate_job_definitions.py: {msg}")


def ensure_jobs_yaml(jobs_yaml_path):
    """Create a sample jobs.yaml with a single job if it doesn't exist."""
    if os.path.exists(jobs_yaml_path):
        return
    with open(jobs_yaml_path, "w") as fp:
        fp.write(JOBS_SAMPLE)
    info(f"created sample {jobs_yaml_path} (edit it to add your jobs)")


def load_jobs(jobs_yaml_path):
    with open(jobs_yaml_path) as fp:
        try:
            data = yaml.safe_load(fp)
        except yaml.YAMLError as err:
            die(f"cannot parse {jobs_yaml_path}: {err}")
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        die(f"{jobs_yaml_path} must contain a top-level 'jobs' list")
    jobs = []
    for count, entry in enumerate(data["jobs"], start=1):
        if not isinstance(entry, dict):
            die(f"{jobs_yaml_path}: job entry #{count} must be a mapping")
        for field in REQUIRED_JOB_FIELDS:
            if not str(entry.get(field, "")).strip():
                die(f"{jobs_yaml_path}: job entry #{count} "
                    f"missing required field '{field}'")
        unknown = set(entry) - set(UT_KNOWN_JOB_FIELDS)
        if unknown:
            die(f"{jobs_yaml_path}: job entry #{count} has unknown field(s): "
                f"{', '.join(sorted(unknown))}")
        jobs.append(entry)
    if not jobs:
        die(f"{jobs_yaml_path}: no jobs defined")
    return data["jobs"]


def load_template(templates_yaml_path, key=TEMPLATE_KEY):
    with open(templates_yaml_path) as fp:
        try:
            data = yaml.safe_load(fp)
        except yaml.YAMLError as err:
            die(f"cannot parse {templates_yaml_path}: {err}")
    if not isinstance(data, dict):
        die(f"{templates_yaml_path} must contain a '{key}' mapping")
    template = data.get(key)
    if not isinstance(template, dict):
        die(f"{templates_yaml_path} must contain a '{key}' mapping "
            f"defining the common job template")
    return template


def load_git_repos(templates_yaml_path):
    """Return the upstream git repositories declared at the top level.

    The ``git_repos:`` section of job_templates.yaml lists the
    repositories that the pre-SCM build step mirrors under /var/cache/git
    (the jobs themselves then check out from those local mirrors). Each
    entry may have:
      - url            (required) upstream repository url
      - name           mirror name (default: url basename minus .git)
      - credentials_id ssh credential id needed to reach the upstream
                       repository (handed to the ssh-agent wrapper)
    """
    with open(templates_yaml_path) as fp:
        try:
            data = yaml.safe_load(fp) or {}
        except yaml.YAMLError as err:
            die(f"cannot parse {templates_yaml_path}: {err}")
    entries = data.get(GIT_REPOS_KEY) or []
    if not isinstance(entries, list):
        die(f"{templates_yaml_path}: '{GIT_REPOS_KEY}' must be a list")
    repos = []
    seen = set()
    for count, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            die(f"{templates_yaml_path}: {GIT_REPOS_KEY} entry #{count} "
                f"must be a mapping")
        url = str(entry.get("url") or "").strip()
        if not url:
            die(f"{templates_yaml_path}: {GIT_REPOS_KEY} entry #{count} "
                f"missing required field 'url'")
        unknown = set(entry) - set(UT_KNOWN_GIT_REPO_FIELDS)
        if unknown:
            die(f"{templates_yaml_path}: {GIT_REPOS_KEY} entry #{count} has "
                f"unknown field(s): {', '.join(sorted(unknown))}")
        name = str(entry.get("name") or "").strip()
        if not name:
            name = url.rstrip("/").rsplit("/", 1)[-1]
            if name.endswith(".git"):
                name = name[:-4]
        if not name or name in seen:
            die(f"{templates_yaml_path}: {GIT_REPOS_KEY} entry #{count}: "
                f"invalid or duplicate repository name '{name}'")
        seen.add(name)
        repos.append((name, url,
                      str(entry.get("credentials_id") or "").strip()))
    return repos


def load_git_mirror_dir(templates_yaml_path):
    """Return the directory that holds the bare repository mirrors.

    The ``git_mirror_dir:`` top level of job_templates.yaml names the
    directory used by both the pre-SCM mirror step and the scm: checkout
    url (injected as {git_mirror_dir} into the job template), so the two
    always point at the same place. It must be writable by the user the
    build runs as. Defaults to /var/cache/git.
    """
    with open(templates_yaml_path) as fp:
        try:
            data = yaml.safe_load(fp) or {}
        except yaml.YAMLError as err:
            die(f"cannot parse {templates_yaml_path}: {err}")
    mirror_dir = str(data.get(GIT_MIRROR_DIR_KEY) or
                     DEFAULT_GIT_MIRROR_DIR).strip().rstrip("/")
    return mirror_dir or "/"


def apply_git_repos(job_definition, repos):
    """Point the ssh-agent wrapper at the repository credentials.

    The mirror clone / update done by the pre-SCM build step talks to the
    upstream repositories, so the ssh credentials declared for them are
    loaded into the ssh-agent started by the ssh-agent-credentials
    wrapper (SSH Agent plugin). That wrapper has to be listed before the
    pre-SCM build step: the agent is started in the wrapper's
    preCheckout(), which is what puts SSH_AUTH_SOCK into the environment
    of the pre-SCM steps. (A credentials-binding wrapper cannot do this -
    it creates its variables in setUp(), after preCheckout.)
    """
    if not repos:
        return
    credentials = []
    for _, _, credentials_id in repos:
        if credentials_id and credentials_id not in credentials:
            credentials.append(credentials_id)
    if not credentials:
        return
    for wrapper in job_definition.get("wrappers") or []:
        if not isinstance(wrapper, dict):
            continue
        ssh_agent = wrapper.get("ssh-agent-credentials")
        if isinstance(ssh_agent, dict):
            ssh_agent["users"] = list(credentials)


def substitute(value, fields):
    """Replace {placeholder} with field values; leave everything else alone.

    Placeholders that are not part of the provided fields (e.g. JJB's
    ${daf-branch} style variables or unknown placeholders) are left
    untouched, so shell / JJB parameter expansions survive generation.

    All remaining literal braces are then doubled, because JJB performs
    its own str.format()-style variable expansion on every string in the
    YAML file and would either fail (allow_empty_variables = False) or
    silently substitute an empty string (allow_empty_variables = True)
    for unrecognized '{name}' style references. Doubled braces are
    rendered back as literal single braces by JJB.
    """
    if isinstance(value, str):
        value = PLACEHOLDER_RE.sub(
            lambda m: str(fields[m.group(1)]) if m.group(1) in fields
            else m.group(0),
            value)
        return value.replace("{", "{{").replace("}", "}}")
    if isinstance(value, dict):
        return {k: substitute(v, fields) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, fields) for v in value]
    return value


def normalize_job_fields(job):
    """Fill in defaults for optional per-job fields."""
    normalized = dict(job)
    normalized["name"] = str(job["name"]).strip()
    normalized["report_email"] = str(job["report_email"]).strip()
    normalized["daf_branch"] = str(job.get("daf_branch") or
                                   DEFAULT_DAF_BRANCH).strip()
    normalized["yang_branch"] = str(job.get("yang_branch") or "").strip()
    normalized["script"] = str(job.get("script") or "").strip()
    normalized["post_script"] = str(job.get("post_script") or
                                    DEFAULT_POST_SCRIPT).strip()
    # Remote trigger (auth) token; defaults to the job name.
    normalized["auth_token"] = str(job.get("auth_token") or
                                   normalized["name"]).strip()
    # Optional per-job default pytest extra args; empty means none. The
    # DAF Pipeline always overrides the parameter with its own
    # extra_args when it triggers the job.
    normalized["extra_args"] = str(job.get("extra_args") or "").strip()
    # Optional per-job default topology file. Falls back to
    # daf/tests/<script>/topo.yaml (the directory of the job's test
    # script; when not set, the script directory is daf/tests/<job
    # name>). The DAF Pipeline overrides the parameter with its own
    # topo_file when it triggers the job and non-empty.
    default_topo_dir = normalized["name"]
    if normalized["script"]:
        script_dir = os.path.basename(os.path.dirname(normalized["script"]))
        if script_dir:
            default_topo_dir = script_dir
    normalized["topo_file"] = str(job.get("topo_file") or
                                  f"daf/tests/{default_topo_dir}/topo.yaml"
                                  ).strip()
    # Optional per-job VPP flag. Accepted as a YAML bool or a true-ish
    # string; becomes the default of the vpp boolean build parameter.
    # The DAF Pipeline overrides the parameter with its own vpp when it
    # triggers the job and the pipeline's vpp is enabled.
    vpp = job.get("vpp", False)
    normalized["vpp"] = "true" if (vpp is True or
                                   (isinstance(vpp, str) and
                                    vpp.strip().lower() in
                                    ("true", "1", "yes", "on"))) else "false"
    # Per-job node label expression. Restricts the job to nodes matching
    # this label; defaults to "daf" when not specified.
    normalized["label"] = str(job.get("label") or "daf").strip()
    return normalized


def generate_definitions(jobs_yaml_path, templates_yaml_path, output_yaml_path):
    ensure_jobs_yaml(jobs_yaml_path)
    jobs = load_jobs(jobs_yaml_path)
    template = load_template(templates_yaml_path)
    # Optional: emit a pipeline job that runs / consolidates all the jobs.
    pipeline_template = load_template(templates_yaml_path, PIPELINE_TEMPLATE_KEY)

    seen = set()
    definitions = []
    repos = load_git_repos(templates_yaml_path)
    if not repos:
        die(f"{templates_yaml_path}: no {GIT_REPOS_KEY} defined")
    mirror_dir = load_git_mirror_dir(templates_yaml_path)
    for job in jobs:
        fields = normalize_job_fields(job)
        name = fields["name"]
        if name in seen:
            die(f"duplicate job name in {jobs_yaml_path}: '{name}'")
        seen.add(name)
        fields["git_mirrors"] = " ".join(f"{repo}={url}"
                                         for repo, url, _ in repos)
        fields["daf_mirror_name"] = repos[0][0]
        fields["git_mirror_dir"] = mirror_dir
        fields["daf_venv_dir"] = f"{mirror_dir}/{repos[0][0]}.git/.env"
        job_definition = substitute(copy.deepcopy(template), fields)
        apply_git_repos(job_definition, repos)
        definitions.append({"job": job_definition})

    if pipeline_template is not None:
        first_fields = normalize_job_fields(jobs[0])
        job_list = ", ".join(f"'{name}'" for name in sorted(seen))
        pipeline_fields = dict(first_fields)
        pipeline_fields["daf_jobs"] = f"[{job_list}]"
        definitions.append({"job": substitute(copy.deepcopy(pipeline_template),
                                              pipeline_fields)})
        info(f"pipeline '{pipeline_template.get('name')}' added, running "
             f"jobs: {sorted(seen)}")

    with open(output_yaml_path, "w") as fp:
        fp.write("# GENERATED FILE - do not edit by hand.\n")
        fp.write(f"# Generated from {os.path.basename(jobs_yaml_path)} and "
                 f"{os.path.basename(templates_yaml_path)}\n")
        fp.write("# by generate_job_definitions.py.\n")
        yaml.safe_dump(definitions, fp, default_flow_style=False, sort_keys=False)
    info(f"generated {len(definitions)} job(s) -> {output_yaml_path}: "
         f"{', '.join(sorted(seen))}")


def list_job_names(output_yaml_path):
    """Print the names of the jobs in the generated definitions file.

    Used by manage_jobs.sh for the 'delete' command.
    """
    with open(output_yaml_path) as fp:
        data = yaml.safe_load(fp)
    for entry in data or []:
        name = (entry.get("job") or {}).get("name")
        if name:
            print(name)


def main(argv):
    jobs_yaml_path = DEFAULT_JOBS_YAML
    templates_yaml_path = DEFAULT_TEMPLATES_YAML
    output_yaml_path = DEFAULT_OUTPUT_YAML
    list_names = False

    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg in ("-h", "--help"):
            print(__doc__)
            print("usage: generate_job_definitions.py [--jobs FILE] "
                  "[--templates FILE] [--output FILE] [--list-names]")
            return 0
        if arg in ("-j", "--jobs"):
            jobs_yaml_path = args.pop(0)
        elif arg in ("-t", "--templates"):
            templates_yaml_path = args.pop(0)
        elif arg in ("-o", "--output"):
            output_yaml_path = args.pop(0)
        elif arg in ("-l", "--list-names"):
            list_names = True
        else:
            die(f"unknown argument: {arg}")

    for path, label in ((templates_yaml_path, "templates"),):
        if not os.path.exists(path):
            die(f"{label} definitions file not found: {path}")

    if list_names:
        if not os.path.exists(output_yaml_path):
            die(f"generated definitions file not found: {output_yaml_path}")
        list_job_names(output_yaml_path)
    else:
        generate_definitions(jobs_yaml_path, templates_yaml_path,
                             output_yaml_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

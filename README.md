# arrcus-daf-jobs

Tools and utilities to create, modify and delete test jobs on a Jenkins
server, using the [jenkins-job-builder](https://pypi.org/project/jenkins-job-builder/)
(JJB) pip package.

Job definitions are described in small, easy-to-maintain YAML files
(jobs.yaml + job_templates.yaml). These are combined by
`generate_job_definitions.py` into a single JJB definitions file
(`jenkins-job-definitions.generated.yaml`) which is then validated and
pushed to the Jenkins server by `manage_jobs.sh`.

## What a pushed job looks like

Every job generated from the template has:

- parameters, in order (`image`, `extra_args`, `topo_file`, `vpp`,
  `debug_console`, `reconnect`, `install_pkgs`, `pause_on_fail`,
  `tb_name`, `daf_repo_dir`, `daf_branch`, `yang_branch`, `script`,
  `label`). `image` is mandatory. `debug_console`, `reconnect`,
  `install_pkgs`, `pause_on_fail` and `tb_name` are Jenkins-only (not
  configurable in jobs.yaml); the rest can also be given per job in
  jobs.yaml where that makes sense.
- a pre-SCM build step (Pre SCM BuildStep plugin) that maintains the
  local git mirrors before anything is checked out, authenticating with
  the ssh credential `github-daf-ssh-key` via the SSH Agent plugin:
  - creates the mirror directory (`git_mirror_dir`, default
    `/var/cache/git`) if it does not exist,
  - for every repository declared in the `git_repos:` section of
    `job_templates.yaml`:
    `git clone --mirror <url> <mirror dir>/<repo name>.git` if the
    mirror does not exist yet, otherwise
    `git --git-dir=<mirror dir>/<repo name>.git remote update --prune`
- a Jenkins `scm:` section (git) that checks the repository out **from
  that local mirror**:
  - `url: file://<git_mirror_dir>/arrcus-daf.git` - the `file://` form is
    required, otherwise git ignores `--depth` for local clones
    ("warning: --depth is ignored in local clones; use file:// instead")
  - shallow clone, `depth: 1`, no tag fetching
  - branch `${daf_branch}`, checked out into `${daf_repo_dir}` (both are
    build parameters, expanded by the git plugin at build time)
  - `wipe-workspace: false` so the checkout (and its `.env` link to the
    shared venv) survives between builds
- a virtual env shared by all jobs at
  `{daf_venv_dir}` = `<git_mirror_dir>/<mirror name>.git/.env` (i.e.
  `/var/cache/jenkins/git/arrcus-daf.git/.env`), so it survives
  re-checkouts and workspace wipes. When it does not exist, the pre-SCM
  step:
  - checks `${daf_branch}` out of the mirror into
    `${WORKSPACE}/${daf_repo_dir}` (the `scm:` checkout runs afterwards
    and updates the same directory),
  - runs `install.sh` there, so the venv is created by the script
    itself, with the interpreter and steps it chooses,
  - moves the resulting `.env` to `{daf_venv_dir}` and rewrites the
    paths recorded inside it (activate scripts, `bin/*` shebangs), so it
    keeps working from the new location,
  - links `<daf-repo-dir>/.env` to it.
- builders that:
  - (re)create that `<daf-repo-dir>/.env` symlink and activate the
    shared venv, so the sources are always the ones checked out under
    `${WORKSPACE}/${daf_repo_dir}` (install.sh does `pip install -e .`)
  - run the pytest script given via the `script` build parameter
    (default: `<daf-repo-dir>/daf/tests/<job name>/run_tests.py`),
    plus the `extra_args` (if set), `--topo-file <topo_file>` (if set),
    `--yang-branch <yang_branch>` (if set), `--vpp` (if the vpp
    checkbox is set), `--debug-console` / `--reconnect` (if those
    checkboxes are set), `--install-pkgs <install_pkgs>` (if set),
    `--tb-name <testbed name>` (from `tb_name`, defaulting to
    `TB<BUILD_NUMBER>`) and the
    fixed report destinations under the workspace root:
    `--log-dir=${WORKSPACE}/logs` (option provided by the daf
    project's conftest), `--junit-xml=${WORKSPACE}/logs/${DAF_TB_NAME}/junit.xml`
    (same per-testbed directory as the logs) and
    `--alluredir=${WORKSPACE}/allure`
  - write the values computed for pytest (`DAF_TB_NAME`, `DAF_IMAGE`,
    `VERSION`, `BUILD_NAME`) to `${WORKSPACE}/.daf-env.properties`,
    then run an EnvInject "inject environment variables" build step on
    it, so the later build steps (pytest, pause, cleanup, result) and
    the publishers get them as real build environment variables. A
    plain shell `export` would only live inside that one step. See
    [Injected build variables](#injected-build-variables).
  - pause before cleanup when the `pause_on_fail` checkbox is set and
    pytest failed: the build writes `${WORKSPACE}/.daf-pytest.rc` (so the
    failure does not skip the following steps), then waits up to 3 hours
    for a resume marker next to the mirrors
    (`<git_mirror_dir>/.pause/<BUILD_TAG>.resume`; `touch` it to resume).
    The build holds the executor while paused, and aborting it skips the
    cleanup step.
  - destroy the testbed (`daf destroy --tb-name ...`) using the
    EnvInject-provided `DAF_TB_NAME` / `topo_file`, falling back to
    `TB<BUILD_NUMBER>` if `DAF_TB_NAME` is missing
  - finally re-raise the deferred pytest exit code, so the build result
    still reflects the test run
- publishers that:
  - publish the JUnit results written by pytest
    (`logs/${DAF_TB_NAME}/junit.xml`)
  - send an email report to the configured recipients
  - publish the allure report (from `allure/`, falling back to the old
    `<daf-repo-dir>/daf/allure-results/`)
  - send Slack notifications to the `daf-jenkins` channel (bot user,
    token from the `daf-jenkins-slack-token` credential) on success,
    aborted, unstable, failure, back-to-normal, regression and repeated
    failure, including the test summary, failed tests and a custom
    message (`IMAGE: ${DAF_IMAGE}: VERSION: ${VERSION} Full Report:
    <${BUILD_URL}allure>`). Publisher-time `${...}` expansion only sees
    Jenkins build variables (parameters, `BUILD_URL`, ...); `DAF_IMAGE`
    and `VERSION` are made available to it by the EnvInject step
    described above.
  - archive the build artifacts (`**/*.xml`, `**/*.log`, `allure/**`)

### Injected build variables

The prepare step (after the docker image is resolved) writes these to
`${WORKSPACE}/.daf-env.properties`, and the EnvInject step right after it
turns them into build environment variables - visible to the remaining
build steps and to the post-build publishers:

| Variable | Value | Used by |
|---|---|---|
| `DAF_TB_NAME` | `tb_name` parameter, or `TB<BUILD_NUMBER>` when empty | pytest `--tb-name`, log/JUnit directories, `daf destroy` |
| `DAF_IMAGE` | the docker image (imported first if an `.xz` archive was given) | pytest `--image`, Slack message |
| `VERSION` | version from the image's `build_info_cfg.json` | Slack message, build name |
| `BUILD_NAME` | `<version> - <JOB_NAME>` | build name setter |

Two details matter when editing this:

- the testbed name is injected as `DAF_TB_NAME`, **not** `TB_NAME`.
  Jenkins' `EnvVars` is case-insensitive, so an injected `TB_NAME` would
  collide with the `tb_name` build parameter (empty unless overridden)
  and the parameter value wins - `TB_NAME` then never appears in the
  shell. `DAF_IMAGE` / `VERSION` / `BUILD_NAME` have no matching
  parameter.
- the values are written unquoted (`printf 'K=%s\n'`). EnvInject parses
  the file with `java.util.Properties`, which does not strip surrounding
  quotes, so `K="v"` becomes the value `"v"` (quotes included).

## The DAF Pipeline job

In addition to the per-job definitions, the generator also emits a
single pipeline job named **DAF Pipeline**:
- it accepts the same knobs as a test job and passes them down:
  `image` (mandatory), `extra_args` and `vpp` (always passed), and
  `topo_file`, `debug_console`, `reconnect`, `install_pkgs`,
  `pause_on_fail`, `tb_name`, `daf_branch`, `yang_branch` and `label`
  (passed only when set / enabled), plus `recipients` for the summary
  email;
- it runs **all** DAF test jobs listed in jobs.yaml in sequence (adding
  a job to jobs.yaml automatically adds it to the pipeline), passing the
  image name down to every job;
- the results of all the jobs are then aggregated: each job's archived
  results (`**/*.xml` and allure results) are copied into per-job
  directories and a consolidated report (`consolidated-report/`
  containing `overview.html` with a status table, and `summary.txt`) is
  archived by the pipeline build;
- a summary email with the per-job status / duration / build links is
  sent to the `recipients` parameter (default: the report_email of the
  first job in jobs.yaml).

Any job failing does not abort the run (`propagate: false`) so that all
jobs always run to completion and the report covers every job; the
pipeline's own result is FAILURE/UNSTABLE if any job failed / was
unstable.


## Remote triggering

Both the test jobs and the DAF Pipeline job have "Trigger builds
remotely" enabled via an auth token:

- test jobs: token defaults to the job name (override with an
  `auth_token` field in jobs.yaml)
- DAF Pipeline: token `daf-pipeline-remote-trigger`

Examples:

```
curl -X POST "https://<jenkins>/job/multicast/buildWithParameters?token=multicast&image=<image name>"
curl -X POST "https://<jenkins>/job/DAF%20Pipeline/buildWithParameters?token=daf-pipeline-remote-trigger&image=<image name>"
```

## Repository layout

| File | Purpose |
|---|---|
| `manage_jobs.sh` | Entry point. Sets up the virtual env, generates the definitions, validates them and pushes / test / deletes jobs on Jenkins. |
| `generate_job_definitions.py` | Python script that generates `jenkins-job-definitions.generated.yaml` from `jobs.yaml` and `job_templates.yaml` (one job per jobs.yaml entry + the DAF Pipeline job). |
| `jobs.yaml` | List of jobs with per-job information (name, daf branch, report email, optional script / topo file / extra args / label overrides). |
| `job_templates.yaml` | All the common pieces (parameters, `git_repos` declaration, scm, pre-SCM mirror step, builders, publishers) plus the DAF Pipeline template. |
| `jenkins.conf` | Jenkins server + job-builder configuration (INI file with `[job_builder]` and `[jenkins]` sections). Edit with your server details. |
| `jenkins.sample.conf` | Fully documented sample of `jenkins.conf`. |
| `jenkins-job-definitions.generated.yaml` | Generated JJB definitions - do not edit by hand. |
| `nodes.yaml` | Jenkins executor node definitions, pushed with `./manage_jobs.sh push-nodes`. |
| `push_nodes.py` | Python script that pushes `nodes.yaml` to the Jenkins server (via the python-jenkins REST API). |

## Requirements

- Python 3.x
- [uv](https://docs.astral.sh/uv/) (used to create the virtual env and
  install packages). If `.env` is missing, `manage_jobs.sh` creates it
  with `uv venv .env` and installs JJB with
  `uv pip install jenkins-job-builder` (plus `setuptools<81`, which
  JJB's plugin loader requires on recent Python versions).

Jenkins server plug-ins used by the generated jobs:

- Git plugin (scm declaration in the template)
- **SSH Agent** plugin (`ssh-agent-credentials` wrapper) - starts an
  ssh-agent for the build and loads `github-daf-ssh-key`, so the pre-SCM
  mirror step can authenticate against GitHub. The wrapper must be
  listed *before* `pre-scm-buildstep`: the agent is started in the
  wrapper's `preCheckout()`, which is what makes `SSH_AUTH_SOCK`
  visible to the pre-SCM steps. The credentials-binding wrapper cannot
  be used instead - it creates its variables in `setUp()`, after
  `preCheckout`.
- **Pre SCM BuildStep** plugin (`preSCMbuildstep`) - runs the
  mirror / checkout build step before the SCM phase
- **Mailer** plugin (the core `- email:` publisher; the report is a
  plain Jenkins mail, not an email-ext publisher)
- **Allure** plugin (`allure` publisher for the `allure/` results)
- **Build Name and Description Setter** plugin (`build-name-setter`,
  sets the build display name to `<version> - <job name>`)
- **Pipeline** plugin (for the DAF Pipeline job)
- **JUnit** plugin (`junit` publisher for `logs/<testbed>/junit.xml`)
- **EnvInject** plugin (`inject` build step): turns the values written to
  `.daf-env.properties` by the prepare step into build environment
  variables, so later steps and the publishers (Slack) can use
  `DAF_TB_NAME` / `DAF_IMAGE` / `VERSION` / `BUILD_NAME`
- **Slack** plugin (`slack` publisher, needs the
  `daf-jenkins-slack-token` secret text credential and the bot user
  configured for the `daf-jenkins` channel)

The executor running the jobs needs a writable mirror directory
(`git_mirror_dir` in `job_templates.yaml`, currently
`/var/cache/jenkins/git`; it must be writable by the user the build runs
as - the node's remoteFS is a safe choice) and `git` available in
`PATH`, plus a writable `$HOME` for the ssh host key
(`StrictHostKeyChecking=accept-new`).

## Setup

1. Configure the Jenkins server:

   ```
   cp jenkins.sample.conf jenkins.conf
   # edit jenkins.conf: url, user, password (API token)
   ```

2. Describe your jobs in `jobs.yaml`:

   ```yaml
   jobs:
     - name: daf-ace-regression
       daf_branch: master
       report_email: premkumar@arrcus.com
   ```

   Field reference:
   - `name` (required) - name of the Jenkins job
   - `report_email` (required) - recipient of the email report
   - `daf_branch` - git branch of the arrcus-daf repo (default: `main`)
   - `yang_branch` - yang models branch; when set, `--yang-branch
     <yang_branch>` is appended to the pytest command line (optional;
     when empty, no argument is passed; the DAF Pipeline overrides it
     when the pipeline's own `yang_branch` parameter is non-empty)
   - `extra_args` - default extra arguments appended to the pytest
     command line (optional; the DAF Pipeline always overrides it with
     the pipeline's own `extra_args`, so pipeline runs inherit them)
   - `topo_file` - default topology file passed to pytest as
     `--topo-file` (optional; defaults to `daf/tests/<script>/topo.yaml`;
     the DAF Pipeline overrides it when the pipeline's own `topo_file`
     parameter is non-empty)
   - `vpp` - default state of the `vpp` build parameter (boolean,
     optional; adds `--vpp` to the pytest command line when enabled;
     the DAF Pipeline passes its own `vpp` down when enabled)
   - `script` - pytest script to run; if not specified the job defaults
     to `daf/tests/<job name>/run_tests.py` inside the daf repo
   - `post_script` - accepted for compatibility but currently unused: no
     build step runs it (the pytest step only gets the arguments listed
     above)
   - `auth_token` - token used to trigger the job remotely
     (`/buildWithParameters?token=<token>`); defaults to the job name

   (If `jobs.yaml` is deleted, `manage_jobs.sh` re-creates it with a
   single sample job.)

## Usage

```
./manage_jobs.sh [options] [command]
```

Commands:

| Command | Description |
|---|---|
| `push` (default) | Generate the job definitions and create / update them on the Jenkins server. |
| `push-nodes` | Push the executor node info from `nodes.yaml` to the Jenkins server (create / update the nodes). |
| `test` (`validate`) | Validate the generated definitions without contacting Jenkins. |
| `delete` (`remove`) | Delete the jobs listed in the generated definitions from the Jenkins server. |
| `generate` | Only (re-)generate `jenkins-job-definitions.generated.yaml`. |
| `setup-env` | Create `.env` and install jenkins-job-builder into it. |
| `recreate-env` | Delete and re-create `.env`. |
| `shell` | Interactive shell with `.env` activated. |

Options:

| Option | Description |
|---|---|
| `-c`, `--conf FILE` | Jenkins config file (default: `jenkins.conf`) |
| `-j`, `--jobs FILE` | Jobs definition file (default: `jobs.yaml`) |
| `-t`, `--templates FILE` | Job templates file (default: `job_templates.yaml`) |
| `-n`, `--nodes FILE` | Node definitions file (default: `nodes.yaml`) |
| `-o`, `--output FILE` | Generated output file (default: `jenkins-job-definitions.generated.yaml`) |
| `-v`, `--verbose` | Debug logging for jenkins-job-builder |
| `-h`, `--help` | Show help |

Examples:

```
./manage_jobs.sh test            # validate only (dry run)
./manage_jobs.sh                 # push definition from jenkins.conf
./manage_jobs.sh -c other.conf   # push using a different conf
./manage_jobs.sh delete          # remove the jobs from the server
```

### Suspending the email report

The email publisher is part of the generated job config, so there is no
per-build switch. To suspend it temporarily, comment out the
`- email:` publisher in the `publishers:` section of
`job_templates.yaml` and push; the push removes the publisher from the
Jenkins jobs. Uncomment it and push again to resume. The DAF Pipeline
sends its summary with a `mail` step in its DSL, so comment that out as
well if the pipeline should stop mailing too.

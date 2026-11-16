#!/usr/bin/env python3
"""Push Jenkins executor node definitions to the Jenkins server.

Reads the node definitions from nodes.yaml and pushes them to the
Jenkins server configured in the jenkins-job-builder INI config file
(jenkins.conf): nodes that do not exist yet are created, existing nodes
are updated with the configuration from nodes.yaml (upsert). Nodes
present on the server but not listed in nodes.yaml are left untouched.

Requires the python-jenkins package (installed together with
jenkins-job-builder in the repo's .env virtual env).
"""

import argparse
import configparser
import json
import sys
import xml.etree.ElementTree as ET
from typing import NoReturn

try:
    import jenkins
    import requests
    import yaml
except ImportError as err:
    print(f"push_nodes.py: error: missing required package '{err.name}' "
          f"(recreate the virtual env: ./manage_jobs.sh recreate-env)",
          file=sys.stderr)
    sys.exit(1)
VPP_TRUE = ("true", "1", "yes", "on")
SSH_PORT_DEFAULT = 22

REQUIRED_NODE_FIELDS = ("name", "launcher")
UT_KNOWN_NODE_FIELDS = (
    "name", "description", "num_executors", "labels", "exclusive",
    "remote_dir", "launcher")
REQUIRED_LAUNCHER_FIELDS = {"ssh": ("host", "credentials_id"), "jnlp": ()}
UT_KNOWN_LAUNCHER_FIELDS = (
    "type", "host", "port", "credentials_id", "jvm_options")


def die(msg) -> NoReturn:
    print(f"push_nodes.py: error: {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg):
    print(f"push_nodes.py: {msg}")


def bool_field(value):
    return str(value).strip().lower() in VPP_TRUE


def parse_yaml(path):
    if not path:
        die("nodes definitions file not specified")
    try:
        with open(path) as fp:
            data = yaml.safe_load(fp)
    except FileNotFoundError:
        die(f"nodes definitions file not found: {path}")
    except yaml.YAMLError as err:
        die(f"cannot parse {path}: {err}")
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        die(f"{path} must contain a top-level 'nodes' list")
    return data["nodes"]


def validate_node(node, count, path):
    if not isinstance(node, dict):
        die(f"{path}: node #{count} must be a mapping")
    for field in REQUIRED_NODE_FIELDS:
        if field not in node or (isinstance(node[field], str)
                                 and not node[field].strip()):
            die(f"{path}: node #{count} missing required field '{field}'")
    unknown = set(node) - set(UT_KNOWN_NODE_FIELDS)
    if unknown:
        die(f"{path}: node #{count} has unknown field(s): "
            f"{', '.join(sorted(unknown))}")
    launcher = node["launcher"]
    if not isinstance(launcher, dict):
        die(f"{path}: node #{count} 'launcher' must be a mapping")
    ltype = str(launcher.get("type", "")).strip().lower()
    if ltype not in REQUIRED_LAUNCHER_FIELDS:
        die(f"{path}: node #{count}: invalid launcher type '{ltype}' "
            f"(expected 'ssh' or 'jnlp)")
    for field in REQUIRED_LAUNCHER_FIELDS[ltype]:
        if field not in launcher or (isinstance(launcher[field], str)
                                     and not launcher[field].strip()):
            die(f"{path}: node #{count}: launcher type '{ltype}' "
                f"missing required field '{field}'")
    unknown = set(launcher) - set(UT_KNOWN_LAUNCHER_FIELDS)
    if unknown:
        die(f"{path}: node #{count}: launcher has unknown field(s): "
            f"{', '.join(sorted(unknown))}")
    return ltype


def build_config_xml(node, ltype):
    """Build the <slave> configuration XML for a node."""
    name = str(node["name"]).strip()
    description = str(node.get("description", "")).strip()
    num_executors = int(node.get("num_executors", "") or 1)
    labels = " ".join(str(node.get("labels", "")).split())
    exclusive = bool_field(node.get("exclusive", False))
    remote_dir = str(node.get("remote_dir", "")).strip()
    if not remote_dir:
        die(f"node '{name}': missing required field 'remote_dir'")

    mode = "EXCLUSIVE" if exclusive else "NORMAL"

    slave = ET.Element("slave")
    ET.SubElement(slave, "name").text = name
    ET.SubElement(slave, "description").text = description
    ET.SubElement(slave, "remoteFS").text = remote_dir
    ET.SubElement(slave, "numExecutors").text = str(num_executors)
    ET.SubElement(slave, "mode").text = mode
    ET.SubElement(
        slave, "retentionStrategy",
        {"class": "hudson.slaves.RetentionStrategy$Always"})
    launcher_el = ET.SubElement(
        slave, "launcher",
        {"class": jenkins.LAUNCHER_SSH if ltype == "ssh"
         else jenkins.LAUNCHER_JNLP})
    if ltype == "ssh":
        launcher = node["launcher"]
        ET.SubElement(launcher_el, "port").text = str(
            launcher.get("port", "") or SSH_PORT_DEFAULT)
        ET.SubElement(launcher_el, "host").text = str(
            launcher["host"]).strip()
        ET.SubElement(launcher_el, "credentialsId").text = str(
            launcher["credentials_id"]).strip()
        jvm_options = str(launcher.get("jvm_options", "")).strip()
        if jvm_options:
            ET.SubElement(launcher_el, "jvmOptions").text = jvm_options
    ET.SubElement(slave, "labelString").text = labels
    ET.SubElement(slave, "nodeProperties")

    ET.indent(slave)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' \
        + ET.tostring(slave, encoding="unicode")


def build_create_form_data(node, ltype):
    """Build the form-data dict for POST /computer/doCreateItem."""
    name = str(node["name"]).strip()
    description = str(node.get("description", "")).strip()
    num_executors = int(node.get("num_executors", "") or 1)
    labels = " ".join(str(node.get("labels", "")).split())
    exclusive = bool_field(node.get("exclusive", False))
    remote_dir = str(node.get("remote_dir", "")).strip()

    mode = "EXCLUSIVE" if exclusive else "NORMAL"

    launcher = {
        "stapler-class": (jenkins.LAUNCHER_SSH if ltype == "ssh"
                          else jenkins.LAUNCHER_JNLP),
    }
    if ltype == "ssh":
        ssh_cfg = node["launcher"]
        launcher["port"] = str(int(ssh_cfg.get("port", "") or SSH_PORT_DEFAULT))
        launcher["host"] = str(ssh_cfg["host"]).strip()
        launcher["credentialsId"] = str(ssh_cfg["credentials_id"]).strip()
        jvm_options = str(ssh_cfg.get("jvm_options", "")).strip()
        if jvm_options:
            launcher["jvmOptions"] = jvm_options

    inner = {
        "nodeDescription": description,
        "numExecutors": num_executors,
        "remoteFS": remote_dir,
        "labelString": labels,
        "mode": mode,
        "retentionStrategy": {
            "stapler-class": "hudson.slaves.RetentionStrategy$Always"},
        "nodeProperties": {"stapler-class-bag": "true"},
        "launcher": launcher,
    }

    return {
        "name": name,
        "type": jenkins.NODE_TYPE,
        "json": json.dumps(inner),
    }


def push_node(client, node, ltype):
    name = str(node["name"]).strip()

    if not client.node_exists(name):
        info(f"creating node '{name}'")
        form_data = build_create_form_data(node, ltype)
        response = client.jenkins_request(
            requests.Request("POST",
                             client._build_url(jenkins.CREATE_NODE, {}),
                             data=form_data))
    else:
        info(f"updating node '{name}'")
        xml = build_config_xml(node, ltype)
        response = client.jenkins_request(
            requests.Request("POST",
                             client._build_url(jenkins.CONFIG_NODE,
                                               {"name": name}),
                             data=xml.encode("utf-8"),
                             headers={"Content-Type": "application/xml"}))

    if response.status_code != 200:
        die(f"pushing node '{name}' failed: "
            f"HTTP {response.status_code}: {response.text}")


def push_nodes(client, nodes, nodes_path):
    seen = set()
    pushed = 0
    for count, node in enumerate(nodes, start=1):
        ltype = validate_node(node, count, nodes_path)
        name = str(node["name"]).strip()
        if name in seen:
            die(f"{nodes_path}: duplicate node name: '{name}'")
        seen.add(name)
        push_node(client, node, ltype)
        pushed += 1
    info(f"pushed {pushed} node(s) -> {client.server}")


def load_jenkins_client(conf):
    cp = configparser.ConfigParser()
    try:
        with open(conf) as fp:
            cp.read_file(fp)
    except FileNotFoundError:
        die(f"jenkins conf file not found: {conf}")
    section = cp["jenkins"]
    url = section.get("url", "").strip()
    if not url:
        die(f"{conf}: [jenkins] section needs a 'url'")
    password = section.get("password", "").strip()
    if password.startswith("REPLACE-ME"):
        die(f"{conf}: password is still the placeholder; edit "
            f"jenkins.conf (see jenkins.sample.conf)")
    user = section.get("user", "").strip()
    if not user:
        die(f"{conf}: [jenkins] section needs a 'user'")
    client = jenkins.Jenkins(url, username=user, password=password)
    try:
        client.get_whoami()
    except Exception as err:
        die(f"cannot reach the Jenkins server '{url}' "
            f"with user '{user}': {err}")
    info(f"connected to the Jenkins server {url}")
    return client


def main(argv):
    parser = argparse.ArgumentParser(
        description="Push Jenkins executor node definitions "
                    "(nodes.yaml) to the Jenkins server.")
    parser.add_argument("-c", "--conf", default="jenkins.conf",
                        help="Jenkins server config file "
                             "(default: jenkins.conf)")
    parser.add_argument("-n", "--nodes", default="nodes.yaml",
                        help="node definitions file (default: nodes.yaml)")
    args = parser.parse_args(argv)

    nodes = parse_yaml(args.nodes)
    client = load_jenkins_client(args.conf)
    push_nodes(client, nodes, args.nodes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

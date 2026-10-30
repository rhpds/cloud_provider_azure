#!/usr/bin/env python3
"""Run real Ansible modules against the synthetic Azure CLI; never contacts Azure."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    collection = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='agnosticd-azure-test-') as tmp:
        root = Path(tmp)
        collections = root / 'ansible_collections' / 'agnosticd'
        collections.mkdir(parents=True)
        (collections / 'cloud_provider_azure').symlink_to(collection, target_is_directory=True)
        output = root / 'output'
        output.mkdir()
        env = dict(os.environ)
        env.update(
            PATH=str(collection / 'tests/integration') + os.pathsep + env.get('PATH', ''),
            AZURE_TEST_STATE=str(root / 'state.json'),
            ANSIBLE_LOCAL_TEMP=str(root / 'local'),
            ANSIBLE_REMOTE_TEMP=str(root / 'remote'),
            ANSIBLE_COLLECTIONS_PATH=str(root) + os.pathsep + env.get('ANSIBLE_COLLECTIONS_PATH', ''),
        )
        command = [sys.executable, '-m', 'ansible.cli.playbook',
                   str(collection / 'tests/integration/provider.yml'),
                   '-e', 'output_dir=' + str(output),
                   '-e', 'ansible_python_interpreter=' + sys.executable]
        template = collection.parents[2] / 'agnosticd-v2/ansible/roles/host_ocp4_installer/templates/install-config.yaml.j2'
        if template.is_file():
            command += ['-e', 'v2_install_config_template=' + str(template)]
        return subprocess.run(command, env=env, cwd=collection, check=False).returncode


if __name__ == '__main__':
    sys.exit(main())

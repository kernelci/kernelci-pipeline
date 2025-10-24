import sys
from urllib.parse import urljoin
import os
import toml
import kernelci.config
import kernelci.storage


SETTINGS = toml.load(os.getenv('KCI_SETTINGS', '/home/kernelci/config/kernelci.toml'))
CONFIGS = kernelci.config.load(
    SETTINGS.get('DEFAULT', {}).get('yaml_config', 'config')
)

def get_storage(storage_config_name):
    storage_config = CONFIGS['storage_configs'][storage_config_name]
    storage_cred = SETTINGS['storage'][storage_config_name]['storage_cred']
    return kernelci.storage.get_storage(storage_config, storage_cred)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Command line argument missing. Specify file name to upload.")
        sys.exit()
    file_name = sys.argv[1]
    cron_configs = SETTINGS.get('cron', {})
    upload_path = cron_configs.get("upload_path")
    file_path = cron_configs.get("file_path")
    if not any([upload_path, file_path]):
        print("Please set 'upload_path' and 'file_path' inside 'cron' section in TOML config file.")
        sys.exit()

    complete_file_path = urljoin(file_path, file_name)
    storage_config_name = SETTINGS.get('DEFAULT', {}).get('storage_config')
    storage = get_storage(storage_config_name)
    print(storage.upload_single((complete_file_path, file_name), upload_path))

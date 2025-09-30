import sys
from urllib.parse import urljoin
import os
import requests


def upload_file(storage_url, token, upload_path, file_name, file_path):
    headers = {
        'Authorization': token,
    }
    complete_file_path = urljoin(file_path, file_name)
    files = {
        'path': upload_path,
        'file0': (file_name, open(complete_file_path, "rb").read()),
    }
    url = urljoin(storage_url, 'upload')
    resp = requests.post(url, headers=headers, files=files)
    resp.raise_for_status()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Command line argument missing. Specify file name to upload.")
        sys.exit()
    file_name = sys.argv[1]
    upload_path = os.getenv("UPLOAD_PATH")
    file_path = os.getenv("FILE_PATH")
    storage_url = os.getenv("STORAGE_URL")
    storage_token = os.getenv("STORAGE_TOKEN")
    if not any([upload_path, file_path, storage_url, storage_token]):
        print("Missing environment variables")
        sys.exit()
    upload_file(storage_url, storage_token, upload_path, file_name, file_path)

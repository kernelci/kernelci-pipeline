FROM kernelci/staging-kernelci

WORKDIR /home/kernelci/callback
COPY requirements.txt .

USER root
RUN pip3 install -r /home/kernelci/callback/requirements.txt

USER kernelci

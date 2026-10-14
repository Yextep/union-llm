"""Descarga reproducible del modelo oficial; confirma tamano y SHA-256."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

project = Path(__file__).resolve().parent
manifest = Path(sys.argv[1]) if len(sys.argv) > 1 else project / 'modelo.json'
model = json.loads(manifest.read_text())
folder = project / 'modelos'
folder.mkdir(exist_ok=True)
destination = folder / model['archivo']
partial = destination.with_suffix(destination.suffix + '.part')
url = 'https://huggingface.co/' + model['repo'] + '/resolve/' + model['revision'] + '/' + model['archivo']
complete = partial.exists() and partial.stat().st_size == model['bytes']
if not destination.exists() and not complete:
    subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error', '--retry', '3',
                    '--connect-timeout', '15', '-C', '-', '--output', str(partial), url], check=True)
if not destination.exists():
    destination = partial
digest = hashlib.sha256()
with destination.open('rb') as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(chunk)
if destination.stat().st_size != model['bytes'] or digest.hexdigest() != model['lfs']['sha256']:
    raise SystemExit('El archivo no coincide con el modelo esperado; no se utilizara.')
if destination == partial:
    partial.replace(folder / model['archivo'])
print('Modelo verificado:', model['archivo'])

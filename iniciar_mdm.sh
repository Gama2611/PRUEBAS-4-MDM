#!/usr/bin/env bash
# Arranque en Linux: ./iniciar_mdm.sh
set -e
cd "$(dirname "$0")/server"
if [ ! -x ../venv/bin/python ]; then
  echo "Primera ejecución: preparando el entorno (necesita Internet)…"
  python3 -m venv ../venv
  ../venv/bin/pip install --upgrade pip
  ../venv/bin/pip install -r requirements.txt
  cp requirements.txt ../venv/requirements.instalado
fi
if ! cmp -s requirements.txt ../venv/requirements.instalado; then
  echo "Actualizando componentes del MDM…"
  ../venv/bin/pip install -r requirements.txt
  cp requirements.txt ../venv/requirements.instalado
fi
exec ../venv/bin/python iniciar_mdm.py

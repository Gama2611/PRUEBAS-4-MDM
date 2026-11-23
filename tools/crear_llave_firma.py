"""Crea, UNA SOLA VEZ, la llave de firma de las apps Android (agente MDM y ADIPACK Avisos).

Genera un keystore PKCS#12 (alias «mdm»), lo guarda FUERA del proyecto (Documentos\\ADIPACK_MDM_LLAVE)
para que nunca se suba a GitHub, y escribe un archivo con los 4 Secrets listos para pegar en GitHub.

Si la llave ya existe NO la reemplaza: cambiar la llave obliga a reinstalar las apps en todas las tablets.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
import secrets
import string
import subprocess
import sys
from pathlib import Path

ALIAS = "mdm"


def carpeta_destino() -> Path:
    base = Path(os.environ.get("USERPROFILE") or Path.home())
    docs = base / "Documents"
    return (docs if docs.is_dir() else base) / "ADIPACK_MDM_LLAVE"


def asegurar_cryptography():
    try:
        import cryptography  # noqa: F401
        from cryptography.hazmat.primitives.serialization import PrivateFormat  # noqa: F401
        PrivateFormat.PKCS12.encryption_builder  # noqa: B018 - requiere cryptography >= 38
    except (ImportError, AttributeError):
        print("Instalando el componente «cryptography» (necesita Internet, una sola vez)...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "cryptography>=41"])


def crear(destino: Path) -> dict:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, pkcs12
    from cryptography.x509.oid import NameOID

    clave = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(28))
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    nombre = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "ADIPACK MDM"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Grupo Plasticos ADIPACK"),
        x509.NameAttribute(NameOID.COUNTRY_NAME, "VE"),
    ])
    ahora = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(nombre).issuer_name(nombre)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(ahora - dt.timedelta(days=1))
            .not_valid_after(ahora + dt.timedelta(days=365 * 30))
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    # PKCS#12 con cifrado moderno (Java 17 de GitHub Actions y Android Gradle lo leen sin problema).
    cifrado = (PrivateFormat.PKCS12.encryption_builder()
               .kdf_rounds(100_000)
               .key_cert_algorithm(pkcs12.PBES.PBESv2SHA256AndAES256CBC)
               .hmac_hash(hashes.SHA256())
               .build(clave.encode()))
    datos = pkcs12.serialize_key_and_certificates(ALIAS.encode(), key, cert, None, cifrado)

    destino.mkdir(parents=True, exist_ok=True)
    (destino / "adipack-mdm.jks").write_bytes(datos)
    b64 = base64.b64encode(datos).decode()
    huella = hashlib.sha256(cert.public_bytes(Encoding.DER)).hexdigest().upper()
    return {"MDM_KEYSTORE_BASE64": b64, "MDM_KEYSTORE_PASS": clave, "MDM_KEY_ALIAS": ALIAS,
            "MDM_KEY_PASS": clave, "_huella": huella}


def escribir_instrucciones(destino: Path, s: dict) -> Path:
    (destino / "1_MDM_KEYSTORE_BASE64.txt").write_text(s["MDM_KEYSTORE_BASE64"], encoding="utf-8")
    txt = destino / "SECRETS_PARA_GITHUB.txt"
    txt.write_text(f"""SECRETS DE GITHUB PARA FIRMAR LAS APPS ANDROID (agente MDM y ADIPACK Avisos)
============================================================================
Repositorio en GitHub > Settings > Secrets and variables > Actions > New repository secret
Crea estos 4 (el nombre exactamente igual, en mayúsculas):

  Nombre: MDM_KEYSTORE_BASE64
  Valor : TODO el contenido del archivo «1_MDM_KEYSTORE_BASE64.txt» (ábrelo con el Bloc de notas,
          Ctrl+A, Ctrl+C y pégalo). Es un texto largo de una sola línea.

  Nombre: MDM_KEYSTORE_PASS
  Valor : {s['MDM_KEYSTORE_PASS']}

  Nombre: MDM_KEY_ALIAS
  Valor : {s['MDM_KEY_ALIAS']}

  Nombre: MDM_KEY_PASS
  Valor : {s['MDM_KEY_PASS']}

Después: pestaña Actions > «Agente Android» > Run workflow (y lo mismo con «App Avisos Android»).
En Artifacts aparecerá el APK SIN «-debug» (por ejemplo adipack-mdm-agente-1.0.12.apk): ése es el
que se sube al MDM.

Huella SHA-256 del certificado (para verificar):
  {s['_huella']}

IMPORTANTE
 - Guarda esta carpeta en un respaldo (USB o carpeta segura). Si pierdes la llave, las tablets no
   aceptarán actualizaciones de las apps y habría que reinstalarlas.
 - NO subas esta carpeta a GitHub ni la envíes por correo o chat.
 - Usa SIEMPRE esta misma llave. Este programa no la reemplaza si ya existe.
""", encoding="utf-8")
    return txt


def main() -> int:
    destino = carpeta_destino()
    jks = destino / "adipack-mdm.jks"
    print("=" * 70)
    print(" ADIPACK MDM · Llave de firma de las apps Android")
    print("=" * 70)
    if jks.exists():
        print(f"\nYa existe una llave en:\n  {jks}\n")
        print("No se crea otra: cambiarla obligaría a reinstalar las apps en todas las tablets.")
        print(f"Los Secrets para GitHub están en:\n  {destino / 'SECRETS_PARA_GITHUB.txt'}")
        abrir(destino)
        return 0
    asegurar_cryptography()
    s = crear(destino)
    txt = escribir_instrucciones(destino, s)
    print(f"\nLISTO. Llave creada en:\n  {jks}")
    print(f"\nAbre este archivo y sigue los pasos para crear los 4 Secrets en GitHub:\n  {txt}")
    abrir(destino)
    return 0


def abrir(carpeta: Path) -> None:
    if sys.platform.startswith("win"):
        try:
            os.startfile(str(carpeta))  # noqa: S606 - abre la carpeta en el Explorador
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())

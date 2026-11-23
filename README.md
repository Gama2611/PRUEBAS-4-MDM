# ADIPACK MDM

MDM propio, open source y sin licencias. Sirve para las tablets Android (modo kiosco con la app MES/CMMS) y para PCs Windows/Linux. El servidor y la consola están hechos en **Python**.

```
adipack-mdm/
├── server/          Servidor + consola web (FastAPI, SQLAlchemy, SQLite o PostgreSQL)
├── agent-android/   Agente Device Owner (Kotlin; se compila con Android Studio)
├── agent-pc/        Agente Windows/Linux (Python, sólo biblioteca estándar)
└── tools/           provision_adb.py: aprovisiona tablets por USB
```

## Control de equipos (v1.2): bitácora de turnos, inventario y actas

Reemplaza al Excel «Bitácora Control Turnos Tablets» y exporta con **el mismo formato**. El archivo original se usa como plantilla y está en `server/app/formatos/`.

| Pantalla | Hoja del Excel | Funciones |
|---|---|---|
| Bitácora de turnos | Control_Turnos_Tablets | Entrega y devolución por turno, equipos pendientes, filtros, totales y exportación a Excel (incluye las 3 hojas) |
| Inventario | Inventario_Tablets | Equipos con código, serial, área, estatus y enlace a la tablet del MDM; importa el Excel actual y las tablets del MDM |
| Actas de entrega | Acta_Entrega_Individual | Acta por trabajador y equipo, numerada; se imprime desde el navegador o se descarga en Excel |
| Personal | — | Operadores con cédula, cargo, turno y área |
| Empresa y logo | — | Razón social, RIF, ciudad, planta, prefijo de los códigos y **logo.png** (aparece en la consola, las actas y los Excel) |

Una devolución marcada como *Dañada* o *Extraviada* pone el equipo en «Dañado / Fuera de servicio». Las tablas nuevas se crean solas al iniciar y no modifican las del MDM.

### Formato del inventario (v1.3)

La hoja `Inventario_Tablets` usa el formato de la empresa, con 10 columnas: Código Activo, Marca, Modelo, **Serial Fisico**, **Serial del Software**, Área Asignada, Software de Control, Accesorios Incluidos, Estatus Actual y Observaciones Técnicas.

- **Áreas:** EXTRUSIÓN, IMPRESIÓN, SELLADO, MANTENIMIENTO, RESPALDO / TI y **S/A**. En el Excel se escriben en mayúsculas.
- **S/A = sin asignar:** el equipo aún no tiene área ni personal. No se puede entregar en la bitácora. Su acta de entrega le asigna el área y la persona.
- **Importar:** las columnas se reconocen por su título, así que sirve tanto el formato actual como el anterior de 9 columnas. Cada ficha se enlaza sola con su tablet del MDM por el *Serial del Software* o por «Equipo MDM TAB-xx» en observaciones.
- **Actualización:** una base existente recibe sola la columna nueva al iniciar el MDM, y los datos se conservan.

## Avisos a las tablets por MQTT (v1.4), sin Internet

Desde la consola, en **Avisos a tablets**, se envían mensajes que aparecen **a pantalla completa encima de la app MES/CMMS**, con sonido. Se pueden mandar a todas las tablets, a un área del inventario o a una tablet. Cada tablet confirma cuándo lo **recibió** y cuándo alguien tocó **Entendido**.

| Pieza | Dónde | Qué hace |
|---|---|---|
| Servicio MQTT integrado | `server/app/mqtt_broker.py` | Broker MQTT 3.1.1 sin dependencias, puerto **1883**. Arranca solo con el MDM. Maneja usuario y clave por tablet, permisos por tema, QoS 0/1/2, retenidos, Last Will y sesiones persistentes |
| Bus de avisos | `server/app/mqtt_bus.py` | Envía los avisos y registra las confirmaciones en la base. Reenvía lo pendiente cuando una tablet vuelve y permite crear avisos desde otros sistemas (`adipack/enviar`) |
| App **ADIPACK Avisos** | `avisos-android/` | App Kotlin sin dependencias, con cliente MQTT propio. Tiene servicio en primer plano, alerta a pantalla completa, sonido según prioridad e historial. La configura el MDM sola |
| Agente MDM | `agent-android/` | Entrega la configuración de la app, le concede el permiso de notificaciones y la mantiene encendida. Como Device Owner, abre la alerta encima del kiosco |
| Compilación | `.github/workflows/avisos-android.yml` | Igual que el agente: usa los mismos Secrets de firma. Un tag `avisos-v*` la publica en Releases |

**Puesta en marcha:**
1. Ejecuta `INICIAR_MDM.bat`, que instala `paho-mqtt`.
2. Ejecuta `ABRIR_FIREWALL.bat`, que abre los puertos 8000 y 1883.
3. Compila la app y el agente en GitHub Actions.
4. Sube los dos APK en **Aplicaciones**. El del agente se sube marcando *Es el agente MDM*.
5. Marca *ADIPACK Avisos* en *Apps obligatorias* de la política de las tablets.

El detalle está en el capítulo 10 del manual.

**Pruebas:** `tests/test_mqtt_broker.py` prueba el broker con paho-mqtt. `tests/test_avisos.py` prueba de punta a punta con tablets simuladas.

### GitHub Actions: «Failed to find package 'tools'» (corregido en v1.4.1)

`android-actions/setup-android@v3` pedía por defecto el paquete `tools`, que Google ya no publica, y el paso «Android SDK» fallaba antes de compilar. Ahora los dos workflows (agente y avisos) indican los paquetes exactos: `platform-tools`, `platforms;android-35` y `build-tools;35.0.0`. Además, `buildToolsVersion = "35.0.0"` quedó fijo en Gradle.

### Logo

El logo es la bolsa rosada y se usa en:
- el ícono del agente en la tablet (adaptativo para Android 8 y superiores, más los PNG de cada densidad);
- el ícono de notificación (silueta);
- la pestaña de la consola web (`favicon.ico`);
- el acceso directo de Windows que crea `CREAR_ACCESO_DIRECTO.bat`.

## Por qué el agente Android no está en Python

Android sólo permite bloquear un equipo en kiosco, instalar apps sin preguntar o impedir el restablecimiento de fábrica a una app nativa registrada como **Device Owner** (DPC). Ningún MDM puede saltarse esa regla. Por eso el diseño es este:

- **Python (servidor):** guarda las políticas, las apps, el inventario, la cola de comandos, la auditoría y genera el QR. Toda la lógica de negocio está aquí.
- **Kotlin (agente, alrededor de 1.000 líneas):** sólo ejecuta en la tablet lo que manda el servidor. Rara vez tendrás que tocarlo.

## Funciones

| Área | Android (Redmi Pad 2) | PC Windows/Linux |
|---|---|---|
| Enrolamiento | QR de fábrica (tocar 6 veces) o USB/ADB | `agent.py enroll` |
| Kiosco | Lock task de una sola app y lanzador propio que la reabre si se cierra, con protección contra bucles de fallos y PIN de salida | Navegador en `--kiosk` hacia la URL del MES/CMMS, se reabre si lo cierran |
| Apps | Subes el APK, la versión se detecta sola, se instala y actualiza sin intervención y se verifica con SHA-256 | Inventario del software instalado |
| Restricciones | Cámara, capturas, barra de estado, restablecimiento de fábrica, modo seguro, USB, ADB, orígenes desconocidos, Wi-Fi, Bluetooth, usuarios | — |
| Configuración | Wi-Fi corporativo, pantalla siempre encendida mientras carga, ventana de actualizaciones del sistema, permisos automáticos, configuración gestionada (JSON) de la app MES | Scripts PowerShell/Bash (si la política lo permite) |
| Comandos | Sincronizar, bloquear, reiniciar, mensaje, salir del kiosco N minutos, instalar, desinstalar, borrar datos de la app, borrado de fábrica | Sincronizar, bloquear, reiniciar, apagar, mensaje, inventario, script |
| Consola | Panel de equipos (en línea, batería, kiosco, política pendiente), acciones masivas, historial de comandos, auditoría, colores rosado y blanco | igual |

Seguridad: los tokens de los equipos se guardan sólo como hash, las contraseñas usan scrypt, la sesión va firmada y todos los formularios tienen protección CSRF. Hay límite de intentos de login, las acciones peligrosas piden escribir el nombre del equipo y el PIN del kiosco nunca viaja en claro.

---

## 0. Base de datos PostgreSQL `adipack_mdm` (una sola vez)

Requisito: PostgreSQL 13 o superior instalado (el mismo que usan el MES o el CMMS sirve).

- **Windows:** doble clic en `CREAR_BASE_DATOS.bat` y escribe la contraseña del usuario `postgres`. Si PostgreSQL está en otra PC: `CREAR_BASE_DATOS.bat --host 192.168.1.20`. Con `--lectura` crea además un usuario de solo lectura para reportes.
- **Linux:** `cd server && ../venv/bin/python crear_base_datos.py`

El script hace esto:
- Crea el usuario `adipack_mdm_app` (sin privilegios de administrador y con una contraseña aleatoria).
- Crea la base `adipack_mdm` en UTF8, con ese usuario como dueño.
- Cierra el acceso a cualquier otro usuario.
- Fija la zona horaria UTC y tiempos máximos de espera.
- Crea las tablas (los campos JSON se guardan como JSONB) y escribe la conexión en `server/.env`.

Se puede repetir sin perder datos.

- **Respaldo:** `RESPALDAR_BD.bat` usa `pg_dump` y guarda la base y los APKs en `server/respaldos`, conservando los últimos 30. Prográmalo a diario con el Programador de tareas: `RESPALDAR_BD.bat --conservar 30`.
- **Restaurar:** `pg_restore --clean --if-exists --no-owner --role=adipack_mdm_app -h localhost -U postgres -d adipack_mdm ARCHIVO.dump`
- **Si ya probaste con SQLite:** con el MDM apagado, `python server/migrar_sqlite_a_postgres.py` copia los equipos, las políticas y los tokens.
- **Pruebas sobre PostgreSQL:** `MDM_TEST_DATABASE_URL=postgresql+psycopg://... python -m pytest -q` (borra las tablas de esa base, así que úsalo solo en una base de pruebas).

## 0.1 Encender con un clic

- **Windows:** doble clic en `INICIAR_MDM.bat`. La primera vez ejecútalo como Administrador para que abra el Firewall; también necesita Internet para instalar dependencias.
- **Linux:** `./iniciar_mdm.sh`

Genera `.env` con una clave aleatoria, detecta la IP de la PC (la que va dentro del QR), abre la consola en el navegador y deja el servidor encendido. El manual de usuario explica el resto paso a paso.

## 1. Servidor (instalación manual)

```bash
cd server
python -m venv ../venv && source ../venv/bin/activate     # Windows: ..\venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # EDITA: MDM_PUBLIC_URL (IP fija del servidor) y MDM_SECRET_KEY
uvicorn app.main:app --host 0.0.0.0 --port 8000
python -m pytest -q         # 29 pruebas
```

Abre `http://IP:8000`, entra con `admin` / `CambiarYa!2026` y **cambia la contraseña** en *Cuenta*.
Para el logo, copia el PNG de ADIPACK a `server/app/static/logo.png`. Si quieres el tono exacto de rosado, cambia `--rosa` en `static/style.css`.
En producción usa PostgreSQL (`MDM_DATABASE_URL`) y el servicio `adipack-mdm.service`.

## 2. Compilar el agente Android (una sola vez)

1. Abre `agent-android/` en Android Studio (Ladybug o posterior) y deja que sincronice Gradle.
2. Crea la keystore y **guárdala en un lugar seguro**. Si la pierdes, no podrás actualizar el agente sin restablecer las tablets.
   `keytool -genkeypair -v -keystore adipack-mdm.jks -alias mdm -keyalg RSA -keysize 4096 -validity 10000`
3. Compila con `MDM_KEYSTORE_PASS=... MDM_KEY_PASS=... ./gradlew assembleRelease` (o *Build > Generate Signed APK*).
4. En la consola, ve a **Apps**, sube `app-release.apk` y marca **"Es el agente MDM"**. Después sube el APK del MES/CMMS.

## 2.1 Compilar el agente con GitHub Actions (sin Android Studio)

El repositorio incluye `.github/workflows/agente-android.yml`, que compila el agente en la nube de GitHub (gratis en repositorios privados dentro de los minutos del plan) y `servidor.yml`, que corre las pruebas del servidor en SQLite y en PostgreSQL 16.

1. Crea un repositorio **privado** en GitHub y sube la carpeta `adipack-mdm`. El `.gitignore` ya excluye la llave `.jks`, `server/.env`, la base y los respaldos.
2. Crea la llave de firma **una sola vez** con doble clic en `CREAR_LLAVE_FIRMA.bat`. No necesita Java ni Android Studio. La llave queda en `Documentos\ADIPACK_MDM_LLAVE`, fuera del proyecto, y se abre esa carpeta.
3. Abre `SECRETS_PARA_GITHUB.txt`, que está en esa carpeta.
4. En GitHub ve a *Settings > Secrets and variables > Actions > New repository secret* y crea los 4 Secrets con los valores de ese archivo:

| Secret | Valor |
|---|---|
| `MDM_KEYSTORE_BASE64` | Todo el contenido de `1_MDM_KEYSTORE_BASE64.txt` |
| `MDM_KEYSTORE_PASS` | La contraseña que indica el archivo |
| `MDM_KEY_ALIAS` | `mdm` |
| `MDM_KEY_PASS` | La misma contraseña |

   El workflow comprueba los Secrets antes de compilar. Si alguno está mal pegado, se detiene con un mensaje claro. Si faltan, avisa en amarillo que sólo generó el APK debug.
5. Cada `push` que cambie `agent-android/` compila solo. También puedes lanzarlo a mano en *Actions > Agente Android > Run workflow*.
6. Descarga el APK en la ejecución, sección *Artifacts*: **APK-FIRMADO-agente-1.0.N**, un .zip con `adipack-mdm-agente-1.0.N.apk`. Súbelo al MDM marcando "Es el agente MDM". El artefacto `solo-pruebas-debug-…` no es para las tablets.
7. Para una versión oficial crea un tag `agente-v1.0.N`: el APK queda publicado en *Releases*.

Notas:
- El `versionCode` es el número de ejecución del workflow, así que cada compilación puede instalarse como actualización encima de la anterior.
- Sin los Secrets sólo se genera el APK **debug**. Su firma cambia en cada compilación, así que no sirve para producción.
- La llave de los Secrets debe ser **siempre la misma**. Si la cambias, las tablets rechazan la actualización del agente.

## 3. Política para las tablets

En **Políticas**, crea una "+ Política Android":
- Modo kiosco activo, con el paquete de tu APK (por ejemplo `com.adipack.mes`). Para verlo: `aapt dump badging mes.apk | grep package`. También aparece en Apps después de subirlo.
- PIN de salida (por ejemplo, 6 dígitos que sólo conozca Mejora Continua).
- Apps obligatorias: marca el APK del MES/CMMS.
- Wi-Fi de planta (SSID y clave). También viaja dentro del QR.
- Deja "Bloquear depuración ADB" **desactivado** mientras pruebas y actívalo al pasar a producción.

## 4. Enrolar las 7 Redmi Pad 2

En **Enrolar**, elige la política, usa el prefijo `TAB` y pon 7 usos. Te genera el QR.

**Método A (QR):** restablece la tablet de fábrica, toca 6 veces la pantalla de bienvenida, escanea el QR y espera. La tablet descarga el agente, queda como Device Owner, se enrola como `TAB-01`, instala el MES y entra en kiosco.

**Método B (USB, si HyperOS rechaza el QR):**
```bash
python tools/provision_adb.py --server http://IP:8000 --token TOKEN \
       --agent app-release.apk --app mes.apk --all
```
Los requisitos están explicados en la cabecera del script. El principal: la tablet no puede tener ninguna cuenta y necesita activadas "Depuración USB" e "Instalar vía USB".

### Problemas conocidos con Xiaomi

- **Instalación silenciosa en HyperOS 2/3:** hay reportes de que falla en la Redmi Pad 2 incluso con el agente como Device Owner. El agente lo detecta, muestra el diálogo del sistema y te lo informa en la consola. Para no depender de eso, instala el MES por USB (método B, `--app`). Las actualizaciones posteriores intentan instalarse en silencio y, si la ROM pide confirmación, la tablet muestra el aviso.
- **Play Protect:** en Android 14 o superior puede bloquear un agente MDM que no venga de Google Play durante la activación por QR. Si pasa, usa el método B (USB).
- **Prueba primero con UNA tablet** y comprueba en ella el QR, el kiosco, el PIN y una actualización del MES antes de hacer las 7.
- El acceso a HTTPS no es obligatorio en la LAN. Si publicas el servidor fuera de la planta, ponle HTTPS (por ejemplo con Caddy) y cambia `cleartextTrafficPermitted` a `false` en el agente.

### «No se puede configurar el dispositivo. Comunícate con el administrador de TI» (v1.3)

Android muestra ese mismo mensaje para cualquier falla de la activación. La página del QR ahora dice en qué paso falló, en el recuadro **«¿Qué pasó en la activación?»**, que se actualiza solo:

| Lo que ves después de escanear | Causa | Solución |
|---|---|---|
| Nada | La tablet no llega a la PC | 1) Ejecuta `ABRIR_FIREWALL.bat`, que también borra las reglas que bloquean a Python. 2) La tablet y la PC deben estar en la misma red; el Wi-Fi de invitados o con aislamiento de clientes no sirve. 3) Revisa la «Dirección que va dentro del QR». 4) Prueba con «Usar QR sin Wi-Fi». |
| «La descarga del agente se cortó» | Señal débil | Acerca la tablet al router. |
| «Agente descargado completo» y aun así falla | El APK del agente | Sube el APK **release firmado** más reciente de GitHub Actions y genera el QR de nuevo. Si Play Protect lo bloquea, usa el método B (USB). |
| «El MDM rechazó la activación» | El código venció o se agotó | Genera uno nuevo. |
| «Tablet activada» | Todo bien | — |

La **prueba con un celular** usa el QR pequeño de esa misma página. Si al escanearlo con un celular conectado al Wi-Fi de planta se abre «✅ Conexión correcta», la red está bien.

**No hace falta** registrar la tablet en el Inventario para activarla. El inventario es solo para la auditoría; después de activarla, usa «Agregar desde el MDM».

Desde la v1.3, el QR valida el agente por su **firma** (`PROVISIONING_DEVICE_ADMIN_SIGNATURE_CHECKSUM`), que es la forma que recomienda Google para Android 14/15. Así el QR no cambia aunque recompiles el agente con la misma llave.

## 5. PCs

- **Windows:** `powershell -ExecutionPolicy Bypass -File agent-pc\install_windows.ps1 -Server http://IP:8000 -Token TOKEN`
- **Linux:** `python3 agent.py enroll --server ... --token ...` y luego `python3 agent.py run`. Agrégalo al autoarranque de la sesión.

Para el kiosco en PC, crea una política Windows con la URL del MES/CMMS. Es un "kiosco suave" (navegador a pantalla completa que se reabre solo). Si necesitas un bloqueo total de Windows, combínalo con una cuenta local de Windows en *Acceso asignado* (Configuración > Cuentas > Acceso asignado).

## Limitaciones actuales (siguientes pasos)

- Los equipos consultan al servidor cada 60 s (configurable desde 15 s). No hay push, así que un comando tarda como mucho un intervalo en llegar.
- No hay control remoto de pantalla, geolocalización ni Android Enterprise con Play Store gestionada.
- La app MES puede leer la *configuración gestionada* (JSON de la política) con `RestrictionsManager.getApplicationRestrictions()`. Sirve, por ejemplo, para enviarle la URL del servidor o la línea de producción.

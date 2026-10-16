# antilag

**Juega Tibia sobre varias conexiones a internet a la vez, sin lag por picos y sin perder la sesión cuando una se cae.**

> [English version](README.md) · Versión en español

---

## Motivo

### Por qué se creó

En Venezuela la conexión a internet es inestable. Con un cable de fibra o ADSL, el ping a los servidores de Tibia (alojados en AWS, EE. UU.) ronda los 100 ms, que es perfectamente jugable. El problema son los **picos de 500 ms** y los **cortes de varios segundos**: dentro del juego eso significa quedarse congelado frente a un monstruo, y muchas veces morir y perder nivel.

La solución obvia es tener una segunda conexión (Wi-Fi, un hotspot del teléfono) y cambiarse a ella cuando la primera falla. Pero **Tibia no lo permite**. Al cambiar de conexión cambia la IP pública, y el servidor corta la sesión con *"Game session is locked to a different IP"*, que en la práctica es lo mismo que el corte que se quería evitar.

### Qué hace

antilag crea un **túnel entre tu PC y un VPS propio** en EE. UU., cerca del servidor del juego:

- El juego siempre sale a internet **con la IP fija del VPS**, sin importar por qué conexión física viaje cada paquete. Así se puede cambiar de enlace, o usar varios a la vez, sin que Tibia lo note.
- Cada paquete del juego se **envía duplicado por todas tus conexiones** (cable, Wi-Fi, teléfono por USB…). El VPS se queda con la primera copia que llega y descarta las demás. La latencia efectiva es **la del enlace más rápido en cada instante**: un pico o un corte en uno de ellos no se nota.
- **Solo el tráfico del juego** pasa por el VPS. El login y la web de Tibia salen directo con tu IP de casa, porque CipSoft rechaza esos servicios desde IPs de datacenter.

![Diagrama de flujo de datos de antilag](docs/img/dfd.png)

*Diagrama de flujo de datos. En azul, el camino de los paquetes del juego. La fuente editable está en [`docs/antilag-dfd.drawio`](docs/antilag-dfd.drawio).*

| # | Proceso                          | Qué hace                                                                                                                                                                           |
| - | -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 | **Capturar IPs del mundo** | Con el personaje dentro del mundo, lee las conexiones del cliente a los puertos 7171/7172 y obtiene las IPs del servidor de ese mundo. Solo esas IPs entran al túnel.              |
| 2 | **Validar enlaces**        | Manda un PING al VPS por cada conexión a internet. Exige al menos dos: con una sola no hay a dónde conmutar.                                                                      |
| 3 | **Túnel antilag**         | Crea un adaptador de red virtual (Wintun). Cifra cada paquete del juego una sola vez y lo envía por todos los enlaces.                                                             |
| 4 | **Vigilancia**             | Detecta enlaces nuevos (otro teléfono por USB) o que cambiaron de IP (el hotspot que reconecta) y los suma en caliente. También avisa si Tibia se conectó por fuera del túnel.  |
| 5 | **Servidor antilag**       | En el VPS: una sesión aislada por jugador. Se queda con la primera copia de cada paquete, la saca por NAT con la IP fija y replica la respuesta por todos los enlaces del jugador. |

### Por qué funciona así

- **Duplicar en vez de detectar y conmutar.** Cualquier sistema que espera a detectar un problema para cambiar de enlace llega tarde: el pico ya ocurrió. Duplicar protege **durante** el pico. Como el tráfico de Tibia son pocos kbps, duplicarlo casi no gasta datos.
- **Un VPS cerca del mundo.** Con el VPS en la misma región que el servidor de Tibia (us-east), el desvío casi no suma latencia: se midieron 94–100 ms hasta el VPS contra 104 ms directo al mundo.
- **Protocolo propio sobre UDP.** WireGuard resolvería el cambio de IP, pero cada par usa un solo camino a la vez: sirve para conmutar, no para duplicar. antilag usa ChaCha20-Poly1305 con claves por sesión y por jugador, y una ventana anti-replay que a la vez descarta las copias duplicadas.

---

## Logro

- **Ni los picos ni los cortes se notan.** En las pruebas de punta a punta se simuló un pico de 400 ms y luego un corte total en uno de los enlaces con `tc netem`. En modo Duplicado, el juego no pasó de **2 ms** de latencia y no perdió **ningún** mensaje.
- **Desconectar el cable en pleno juego no tira la sesión.** Probado con Tibia real: se desenchufa el cable, el juego sigue por la Wi-Fi sin logout y sin congelarse.
- **Enlaces en caliente.** Un tercer enlace que aparece, o una Wi-Fi que vuelve con otra IP, se suman solos con el juego en marcha, sin perder mensajes.
- **Varios jugadores en un mismo VPS**, aislados entre sí: cada uno con su secreto y su IP en el túnel. Que uno pierda su cable o reinicie no afecta a los demás.
- **Sin bloqueos de CipSoft.** El login sale con la IP de casa y el juego con la del VPS, así que no aparecen el 403 ni el falso *"down for maintenance"*.
- **Uso real**: varios días de juego con resultados excelentes.
- **Calidad**: más de 220 pruebas automáticas, incluidas pruebas de punta a punta con túneles, enlaces y fallas reales dentro de *network namespaces* de Linux.

| Prueba de laboratorio (corte total de un enlace)                    | Peor latencia del juego     |
| ------------------------------------------------------------------- | --------------------------- |
| Modo Duplicado                                                      | ~2 ms (el corte no se nota) |
| Modo Adaptativo                                                     | ~180 ms                     |
| Modo Fallback                                                       | ~640 ms                     |
| Conmutación clásica, sin detección rápida (la primera versión) | 3.155 ms                    |

---

## Tecnología

- **Python 3.10+**, sin dependencias binarias salvo `cryptography`.
- **Criptografía**: ChaCha20-Poly1305 (AEAD) y HKDF-SHA256, con una clave por sesión y por jugador.
- **Windows**: adaptador virtual [**Wintun**](https://www.wintun.net/) (el driver de WireGuard) vía `ctypes`, rutas con `netsh`, secretos cifrados con **DPAPI**.
- **Linux (VPS)**: interfaz **TUN**, servidor `asyncio` con `IP_PKTINFO` (compatible con IPs reservadas), NAT con **iptables** y servicio **systemd**.
- **Interfaz**: [**Flet**](https://flet.dev/) 1.0 (Flutter desde Python).
- **Empaquetado**: `flet pack` (**PyInstaller**): un solo `.exe` que pide administrador al abrirse.
- **Pruebas**: `unittest`, *network namespaces* de Linux y `tc netem` para simular picos, cortes y cambios de IP.
- **Diagramas**: draw.io.

---

## Implementación

Hacen falta dos partes: el **servidor en un VPS** (una vez) y el **panel en cada PC** que va a jugar.

### 1. Servidor (VPS Linux)

**Requisitos:**

- Un VPS con Ubuntu 22.04/24.04, en la región del servidor de tu mundo. Para los mundos en AWS us-east: Ohio o Virginia en AWS, o Nueva York en DigitalOcean. Con el plan más chico sobra.
- El puerto **UDP 51888** abierto en el firewall del proveedor (security group en AWS, Cloud Firewall en DigitalOcean).

**Instalación:**

```bash
git clone https://github.com/mitdua/antilag.git
cd antilag
sudo bash deploy/setup-vps.sh
```

El script se puede volver a correr para actualizar, y hace todo esto:

- instala antilag en `/opt/antilag`;
- configura el reenvío, el NAT y el ajuste de MSS, y los deja persistentes;
- crea el servicio `antilag`;
- crea el primer jugador, `principal`, e **imprime su código de acceso**.

**Jugadores:**

```bash
sudo antilag clients add amigo1        # crea un jugador e imprime su código de acceso
sudo antilag clients list              # quién existe y quién está en línea
sudo antilag clients code amigo1       # vuelve a mostrar su código
sudo antilag clients remove amigo1     # lo revoca al instante
```

El **código de acceso** (`antilag1:…`) contiene el servidor, el jugador y su secreto. Trátalo como una contraseña. Si usas una IP reservada o flotante, fíjala con `sudo antilag clients endpoint IP:51888` antes de repartir códigos.

### 2. PC del jugador (Windows)

#### Opción A: ejecutable (recomendada)

Descarga `antilag.exe` desde la sección **[Releases](https://github.com/mitdua/antilag/releases/latest)** del repositorio y ábrelo. No necesita instalar nada.

> GitHub no recomienda guardar ejecutables dentro del repositorio, pero sí permite adjuntarlos a una **Release** (hasta 2 GB por archivo). El enlace directo a la última versión es
> `https://github.com/mitdua/antilag/releases/latest/download/antilag.exe`.

- Windows pide **permisos de administrador** al abrirlo: hacen falta para crear el adaptador de red.
- Como el ejecutable no tiene firma digital, la primera vez *SmartScreen* puede mostrar "Windows protegió su PC". Entra en **Más información → Ejecutar de todas formas**.

**Generar el `.exe` tú mismo:**

```powershell
git clone https://github.com/mitdua/antilag.git
cd antilag
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
```

El script instala lo necesario (Flet, PyInstaller, cryptography), baja `wintun.dll` verificando su hash y deja **`dist\antilag.exe`** (~65 MB). Con `-SinAdmin` genera una versión de prueba que no pide administrador.

**Publicar una versión:** el `.exe` de las Releases se genera solo con GitHub Actions ([`.github/workflows/release.yml`](.github/workflows/release.yml)). Al subir una etiqueta de versión, el workflow corre las pruebas en Linux y Windows, construye el `.exe` con el mismo script y lo adjunta a una Release nueva:

```bash
git tag v1.0.0
git push origin v1.0.0
```

También se puede correr a mano desde la pestaña **Actions** (*Run workflow*): construye el `.exe` y lo deja como artefacto, sin publicar una Release.

#### Opción B: desde el código, sin generar el `.exe`

Requiere **Python 3.10+**. En una terminal **como administrador**:

```powershell
git clone https://github.com/mitdua/antilag.git
cd antilag
python -m pip install --user cryptography "flet>=1.0"
python -m antilag fetch-wintun          # baja wintun.dll y verifica su hash
python -m antilag panel                 # abre el panel
python -m antilag panel --demo          # panel con enlaces simulados, sin VPS ni administrador
```

**Pruebas:**

```bash
python -m unittest discover -s tests -t .
# En Linux, incluidas las de punta a punta (unos 2 minutos):
ANTILAG_E2E=1 python -m unittest discover -s tests -t .
```

---

## Visual

El panel es un asistente de cuatro pasos. Las capturas son del modo demo (`--demo`), con enlaces simulados.

### Antes de empezar: permisos de administrador

![Aviso de administrador](docs/img/paso-0-admin.png)

Si el panel se abre sin permisos de administrador, lo avisa **en el primer paso**, antes de capturar nada. Con el botón **Reabrir como administrador**, Windows muestra el diálogo de permisos y el panel se abre de nuevo con privilegios. El ejecutable pide estos permisos por sí solo.

### Paso 1: Mundo

![Paso 1: capturar las IPs del mundo](docs/img/paso-1-mundo.png)

Cada mundo de Tibia tiene sus propias IPs, que cambian con el tiempo. Para que antilag sepa qué tráfico llevar por el túnel:

1. Abre Tibia y **entra al mundo** con el personaje que vas a jugar.
2. Pulsa **Capturar IPs**.

![Paso 1: IPs capturadas](docs/img/paso-1-mundo-capturado.png)

3. **Sal del mundo**, a la lista de personajes o cerrando Tibia, y pulsa **Siguiente**. Una conexión ya abierta no puede pasarse al túnel; si Tibia sigue dentro del mundo, el panel no deja avanzar.

Las IPs se guardan solo en memoria: al cerrar el panel se olvidan, porque cambian con cada mundo.

### Paso 2: Servidor

![Paso 2: código de acceso y tipo de conexión](docs/img/paso-2-servidor.png)

- **Código de acceso**: pega el que te dio quien administra el VPS. El panel muestra con qué jugador e IP vas a conectar. Se guarda en tu PC, cifrado con tu usuario de Windows.
- **Tipo de conexión**:
  - **Duplicado** (recomendado): el juego sale siempre por todos los enlaces; ni los picos ni los cortes se notan.
  - **Adaptativo**: un solo enlace mientras esté sano; si un ping se atrasa (~0,3 s), duplica de emergencia.
  - **Fallback**: un solo enlace; si falla, pasa al siguiente. Un corte brusco se nota como un tirón breve.
- **Enlace preferido**: el que lleva lo que no se duplica. Conviene el que no gasta datos móviles.

### Paso 3: Enlaces

![Paso 3: validación de enlaces](docs/img/paso-3-enlaces.png)

El panel manda un PING al VPS por cada conexión a internet y muestra cuánto tarda cada una. Una respuesta válida confirma a la vez que el enlace sale a internet, que el VPS está arriba y que el código es correcto.

- Hacen falta **al menos dos** conexiones que respondan; si no, el panel explica por qué y ofrece **Reintentar**.
- Con dos o más, **Conectar** levanta el túnel.

### Paso 4: Jugar

![Paso 4: jugando con los dos enlaces sanos](docs/img/paso-4-juego.png)

Con el túnel arriba, **entra al mundo** como siempre:

- **Aviso superior**: confirma "Jugando a través del túnel" cuando detecta el juego por el túnel. Se pone en rojo si Tibia se conectó por fuera, por ejemplo porque entraste a otro mundo.
- **Una tarjeta por enlace**:
  - el ping en vivo y su estado (`EMISOR`, `EN ESPERA`, `SOSPECHA`, `CAÍDO`);
  - un gráfico de los últimos dos minutos, con escala fija de 0 a 600 ms y una línea en los 300 ms, el límite a partir del cual Tibia se vuelve injugable;
  - qué porcentaje de las veces ese enlace entrega primero.
- **Contadores**: paquetes enviados y recibidos, cuánto del juego se duplicó y cuántas copias se descartaron.
- **Conmutaciones**: el historial de lo que decidió el panel y por qué.

Así se ve un corte del cable con el juego en marcha:

![Paso 4: corte del cable, el juego sigue por la Wi-Fi](docs/img/paso-4-juego-corte.png)

El cable aparece `CAÍDO`, con el corte en rojo en su gráfico y el pico de antes también visible. El encabezado avisa "1 ENLACE CAÍDO" y la Wi-Fi sigue llevando el juego: **para el jugador no pasó nada**.

**Desconectar** (o cerrar la ventana) baja el túnel y deja la red de Windows como estaba.

---

## Disclaimer

- antilag es un **proyecto personal, sin fines de lucro**, creado para resolver un problema real de conectividad. No se vende ni se ofrece como servicio.
- **No está afiliado a CipSoft GmbH** ni a Tibia. Tibia es una marca registrada de CipSoft GmbH.
- antilag **no modifica, lee ni automatiza** nada del juego: solo transporta los paquetes por varias conexiones, cifrados, y descarta las copias duplicadas. Es la misma categoría que las herramientas comerciales de reducción de lag usadas en muchos juegos. Aun así, **cada usuario es responsable** de usarlo de acuerdo con las reglas y los términos de servicio de Tibia y de su proveedor de internet y de VPS.
- Quien administra el VPS es responsable de su costo, su seguridad y de a quién le reparte códigos de acceso.
- Se ofrece **tal cual, sin garantías** de ningún tipo. El uso es bajo tu propia responsabilidad.

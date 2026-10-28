# enable-banking

Descarga a diario los movimientos de mis cuentas bancarias con [Enable Banking](https://enablebanking.com)
y los guarda en una tabla `transactions` de Supabase (Postgres).

## Contenido

| Fichero | Para qué sirve |
|---|---|
| `auth.py` | Autoriza un banco y guarda la sesión en `sessions.json`. Se ejecuta **en local**. |
| `fetch.py` | Lee los movimientos nuevos y los inserta en Supabase. Se ejecuta en GitHub Actions. |
| `categorias.yml` | Reglas de categorización y de traspasos entre cuentas propias. |
| `rules.py` | Carga y valida `categorias.yml`. |
| `db.py` | Conexión a Postgres con `verify-full`, compartida por `fetch.py` y `backup.py`. |
| `backup.py` | Exporta las tablas `transactions` y `saldos_iniciales` a CSV. |
| `scripts/revisar_ia.py` | Lista los gastos que ha categorizado la IA (o el historial…) para revisarlos. Solo lee. |
| `scripts/corregir.py` | Corrige a mano la categoría de uno o varios gastos (`category_source = 'manual'`). |
| `schema.sql` | Esquema de la base de datos: tabla, índices, RLS, políticas, permisos de cada rol y vistas para Looker Studio. |
| `.github/workflows/fetch-bank.yml` | Lanza `fetch.py`. Sin `schedule`: lo dispara cada día cron-job.org por `workflow_dispatch` (también a mano desde Actions). Publica los avisos. |
| `.github/workflows/vigilar-fetch.yml` | Cada día comprueba que `fetch-bank` ha terminado bien en las últimas 36 h y avisa si no (`scripts/check_fetch.sh`). |
| `.github/workflows/backup.yml` | Copia de seguridad cifrada de `transactions` y `saldos_iniciales` cada domingo (ver más abajo). |
| `.github/workflows/ci.yml` | Tests, pre-commit y comprobaciones de seguridad (pip-audit y zizmor) en cada push y pull request. |
| `.github/workflows/auditoria.yml` | Cada lunes busca vulnerabilidades conocidas en las dependencias (pip-audit) y avisa con un issue. |
| `.pre-commit-config.yaml` | Comprobaciones automáticas antes de cada commit (ver más abajo). |

## Cómo funciona

1. `auth.py <etiqueta> <banco>` abre el flujo de autorización y escribe `sessions.json`.
2. El contenido de `sessions.json` se guarda como secret `EB_SESSIONS` en GitHub.
3. `fetch.py` pide los movimientos desde el último guardado (menos `VENTANA_DIAS` de margen, 30 por defecto, para ver lo que el banco contabiliza tarde; y nunca antes de `CUTOFF_DATE`).
4. Los duplicados se descartan con el índice único `(account, external_id)`.
5. Los traspasos entre cuentas propias se marcan con `is_transfer = true`. Para los informes, usa `where not is_transfer`.
6. Los gastos se clasifican por categoría con reglas sobre la descripción (`categorias.yml`, ver más abajo),
   con las mismas categorías del histórico manual. Los que no coinciden con ninguna regla quedan sin categoría.

## Categorías

Las reglas están en `categorias.yml`; no hace falta tocar el código. Cuando `fetch.py` avise de un
`Gasto sin categoría`, añade una regla en la lista `categorias`:

```yaml
  - categoria: Comida
    patron: 'ALDI|MERCADONA'        # expresión regular, sin distinguir mayúsculas
    ejemplos: ['MERCADONA CENTRO']  # descripción real: el test comprueba que acaba en esta categoría
```

- **El orden importa:** gana la primera regla que coincida, así que una más concreta va antes que otra más general.
- Cada regla necesita al menos un ejemplo. `pytest` comprueba que el fichero es válido y que cada ejemplo acaba en la
  categoría de su regla, lo que también detecta una regla tapada por otra anterior.
- La categoría debe estar en `categorias_permitidas`: un `Comdia` mal escrito hace fallar los tests y sugiere `Comida`.
  Si creas una categoría nueva, añádela primero a esa lista.
- Los traspasos entre cuentas propias están en el bloque `traspasos`.

### Viajes

En el bloque `viajes` de `categorias.yml` van las fechas de cada viaje (`nombre`, `desde`, `hasta`, ambas incluidas).
`viajes.py`, que `fetch.py` llama después de las reglas y antes del historial, pone en `Viajes` todo gasto del banco
de esas fechas (`category_source = 'viaje'`, columna `viaje` con el nombre y vista `v_gasto_por_viaje` para Looker),
salvo lo categorizado a mano y lo de las categorías de `no_cambian`, como alquiler o suscripciones. Se aplica a todo el
histórico en cada fetch: un viaje añadido después, o con las fechas corregidas, se arregla solo. Lo que quede fuera
vuelve a estar sin categoría. El historial y la IA no aprenden de estos gastos.

### Categorización por historial

Lo que las reglas no cubren lo intenta `categorize.py`, que `fetch.py` llama al final de cada ejecución. Aprende qué
palabras van casi siempre con la misma categoría en los gastos ya categorizados (`ALDI` → `Comida`) y las busca en los
gastos sin categoría. Decide solo si la palabra ha aparecido al menos 2 veces y el 90 % de ellas con la misma categoría;
si no, deja el gasto vacío y sale el aviso de siempre. Sin IA ni servicios externos. Medido sobre los datos reales,
resuelve unos 6 de cada 10 gastos del banco y acierta 96 de cada 100.

La columna `category_source` dice de dónde sale cada categoría: `regla`, `historial`, `ia`, `viaje` o `manual` (vacía
en lo anterior).
El historial no aprende de lo que pone él mismo, y una regla puede corregir lo que puso el historial, pero nunca lo
categorizado a mano. Para ver qué haría sin escribir nada: `python categorize.py --dry-run`.

### Categorización con IA

Lo que ni las reglas ni el historial resuelven lo intenta `ia.py` con Claude Haiku (`category_source = 'ia'`, revisable
en Looker). Necesita el secret `ANTHROPIC_API_KEY`; sin él, o si la API falla, no hace nada y `fetch.py` sigue. A mano
(con `SUPABASE_DB_URL`, `SUPABASE_CA_CERT` y `ANTHROPIC_API_KEY`): `python ia.py` categoriza, `python ia.py --dry-run`
solo muestra qué haría y `--verbose` (o `--dry-run`) añade una línea por gasto.

### Revisar y corregir lo que pone la IA

Sin entrar al SQL Editor. En local, con `SUPABASE_CA_CERT` (`..\prod-ca-2021.crt`) y la URL de la base en `SUPABASE_DB_URL`:
`revisar_ia.py` vale con el rol `app_backup` (solo lee) y `corregir.py` necesita `app_fetcher`.

```powershell
python scripts/revisar_ia.py                      # los 30 últimos que ha puesto la IA, con su id
python scripts/revisar_ia.py --categoria Ocio     # solo los de una categoría (también --fuente historial, --limite 100)
python scripts/corregir.py Ocio 123 124 --dry-run # muestra qué cambiaría
python scripts/corregir.py Ocio 123 124           # lo hace: categoría Ocio y category_source = 'manual'
```

Es todo o nada (si un id no existe o la categoría no está en `categorias_permitidas`, no cambia nada) y, si el gasto
estaba en un viaje y la categoría nueva no es Viajes, lo saca del viaje. Lo marcado como `manual` no lo vuelven a tocar
las reglas, los viajes ni la IA, y el historial y la IA aprenden de ello.

### Qué sale en el log

- Una línea por cuenta y, al final, `Resumen: …` con cuentas, movimientos leídos y nuevos, traspasos, categorizados
  por historial y por IA, y gastos sin categoría. En Actions, la misma información sale como tarjeta de resumen de la
  ejecución, con los tokens que ha gastado la IA.
- La IA deja otra línea (`IA: N pendientes → …`) con categorizados, `NO_SE`, descartados, tokens y segundos. Si no
  clasifica más de la mitad de los pendientes (con 4 o más), avisa con un issue: suele ser el modelo o el prompt.
- El detalle de cada gasto va en secciones plegables de Actions (traspasos por pareja, historial, IA). Los fallos de la
  API muestran el estado HTTP y el mensaje, y el del banco indica qué cuentas se habían procesado ya.

**Cómo se prueba:**

- `tests/test_categorize_propiedades.py` (en el CI): con [Hypothesis](https://hypothesis.readthedocs.io) genera cientos
  de historiales y descripciones aleatorios y comprueba que siempre se cumple lo esencial: solo devuelve categorías
  permitidas, decide lo mismo que un cálculo independiente, no le afectan mayúsculas, acentos, números ni el orden del
  historial, y nunca decide con una palabra vista una sola vez.
- `scripts/evaluar_categorizacion.py` (en local, con tus datos y solo lectura) para ver cómo se comporta: validación
  cruzada con cobertura, aciertos y confusiones; `--ruido N` ensucia descripciones reales; `--comparar` prueba otros
  umbrales; `--explicar "TEXTO"` dice qué palabra decidió y con qué recuentos. Usa el rol `app_backup`:

```powershell
$env:SUPABASE_DB_URL = "<URL de app_backup>"
$env:SUPABASE_CA_CERT = Get-Content ..\prod-ca-2021.crt -Raw
python scripts/evaluar_categorizacion.py --ruido 5
```
- La regla nueva se aplica a los movimientos que lleguen desde ese momento. Los ya guardados con categoría no cambian;
  los que no tienen categoría se rellenan al relanzar `fetch.py`, dentro de la ventana de `VENTANA_DIAS` (30 días) que vuelve a leer.

## Secrets de GitHub

Settings → Secrets and variables → Actions:

<!-- secrets:start -->
| Secret | Contenido |
|---|---|
| `SUPABASE_BACKUP_DB_URL` | Cadena de conexión Postgres del rol `app_backup` (solo lectura), para las copias de seguridad |
| `SUPABASE_CA_CERT` | Certificado de la CA de Supabase (contenido del `.crt`), para conectar con `verify-full` |
| `BACKUP_PASSPHRASE` | Contraseña con la que se cifra la copia de seguridad semanal (guárdala también fuera de GitHub) |
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram (@BotFather) para los avisos. Opcional: sin él solo se abre el issue |
| `TELEGRAM_CHAT_ID` | ID del chat al que el bot envía los avisos. Opcional, va junto al token |
| `EB_APP_ID` | ID de la aplicación en Enable Banking |
| `EB_PRIVATE_KEY` | Contenido del `.pem` |
| `EB_SESSIONS` | Contenido de `sessions.json` |
| `SUPABASE_DB_URL` | Cadena de conexión Postgres del rol `app_fetcher` (permisos mínimos sobre `transactions`) |
| `ANTHROPIC_API_KEY` | Clave de la API de Anthropic para categorizar con IA (`ia.py`). Opcional: sin ella no hay categorización con IA |
<!-- secrets:end -->

(Tabla generada por `scripts/gen_readme.py` a partir del workflow.)

El consentimiento del banco caduca a los 90 días: repite `auth.py` y actualiza `EB_SESSIONS`.
`fetch.py` avisa con 14 días de margen.

## Avisos

El workflow `fetch-bank` abre issues en este repo (te llegan por email), y los cierra solo cuando el problema desaparece:

| Issue | Cuándo |
|---|---|
| `enable-banking: avisos` | Un consentimiento caduca en menos de 14 días, o una cuenta lleva más de `STALE_DAYS` (30 por defecto) sin movimientos nuevos |
| `enable-banking: gastos sin categoría` | Hay `SIN_CATEGORIA_AVISO` (10 por defecto) o más gastos sin categoría en todo el histórico. Por Telegram solo llega los lunes |
| `enable-banking: avisos` (saldo) | El saldo contabilizado que da el banco difiere en más de `SALDO_TOLERANCIA` (1 € por defecto) del que sale de `saldos_iniciales` + movimientos: puede faltar o sobrar un movimiento. Solo se comprueban las cuentas con fila en `saldos_iniciales`, en EUR |
| `enable-banking: avisos` (moneda) | Un movimiento llega en una moneda que no es EUR: se guarda con su moneda, pero las vistas de Looker solo suman EUR |
| `enable-banking: fallo en el workflow` | La ejecución ha fallado (consentimiento caducado, base de datos inaccesible…). Si un banco falla, el resto de cuentas se guardan igual y la ejecución falla al final; el issue lista las cuentas que no se pudieron leer |
| `enable-banking: dependencias vulnerables` | El workflow semanal `auditoria` (pip-audit) ha encontrado una vulnerabilidad conocida en las dependencias |
| `enable-banking: el fetch no se ha ejecutado` | Hace más de 36 h que `fetch-bank` no termina bien, p. ej. porque cron-job.org ha dejado de lanzarlo (token de GitHub caducado, cuenta…) |

Si están los secrets `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`, el mismo aviso llega también por Telegram, cada día
mientras el problema siga (un fallo de Telegram no tumba el workflow). Para configurarlo: crea el bot con @BotFather,
escríbele un mensaje, abre `https://api.telegram.org/bot<TOKEN>/getUpdates` y copia `message.chat.id`.

Si una cuenta de ahorro casi no tiene movimientos, sube `STALE_DAYS` en el workflow. Las llamadas a la API del banco
se reintentan hasta 3 veces con espera creciente ante errores 429 y 5xx; lo mismo con la API de Anthropic (también 529, sobrecargada).
Cada job tiene un tiempo máximo (`timeout-minutes`), las consultas a la base se cortan a los 2 minutos (`statement_timeout`) y
`fetch-bank` y `backup` no se solapan (`concurrency`): un lanzamiento manual espera a que acabe el de cron-job.org.

## Vistas para Looker Studio

`schema.sql` define cuatro vistas (con `security_invoker`, así que el rol `looker` sigue sujeto a la RLS de `transactions`).
Ninguna cuenta los traspasos entre cuentas propias como gasto ni ingreso:

| Vista | Columnas |
|---|---|
| `v_gasto_mensual_categoria` | `mes`, `account`, `categoria` (`Sin categoría` si falta), `gasto` (en positivo), `movimientos` |
| `v_ahorro_mensual` | `mes`, `ingresos`, `gastos`, `ahorro` (ingresos − gastos) |
| `v_saldo_mensual` | `account`, `mes`, `saldo` al final del mes |
| `v_saldo_actual` | `account`, `saldo` ahora, `ultimo_movimiento` |

Las vistas solo suman movimientos en EUR (sin conversión de moneda, otras monedas darían cifras falsas).
El rol `looker` lee `transactions` por columnas y sin `raw` (el JSON completo del banco): en Looker Studio no puede usar `select *`.
Las vistas de saldo parten de la tabla `saldos_iniciales` (una fila por cuenta): el saldo del banco menos la suma de
lo guardado. Sin esa fila la cuenta no sale (es el caso de `historico`, que no es una cuenta real). Los importes se
rellenan a mano en el SQL Editor y no se guardan en el repo. Los traspasos sí cuentan en el saldo, porque mueven dinero.
Para aplicar el esquema en Supabase, ejecuta `schema.sql` en el SQL Editor (es idempotente) y añade las vistas como fuentes en Looker.

## Copia de seguridad

`backup.yml` exporta `transactions` y `saldos_iniciales` (esta última se rellena a mano y no se puede regenerar) a CSV cada domingo, los cifra con AES-256 (`BACKUP_PASSPHRASE`) y los guarda
como *artifact* de la ejecución durante 90 días (es el máximo de GitHub). Para recuperarla: Actions → backup →
la ejecución → descarga `backup-transactions` y descifra:

```bash
gpg --decrypt transactions-AAAA-MM-DD.csv.gpg > transactions.csv
gpg --decrypt saldos_iniciales-AAAA-MM-DD.csv.gpg > saldos_iniciales.csv
```

Los CSV no incluyen el esquema: para restaurar, aplica `schema.sql` y carga cada CSV con `\copy` (ver `backup.py`).
La copia usa el rol `app_backup`, que solo puede leer.
Guarda la contraseña también fuera de GitHub: si la pierdes, las copias no se pueden abrir.

## Tests

```bash
uv run --python 3.12 --with pytest --with-requirements requirements.txt pytest
```

Los tests de `tests/test_integration.py` ejecutan el SQL real de `fetch.py` y `backup.py` (incluidos RLS y los permisos de
cada rol) sobre un Postgres desechable y se saltan si no hay `TEST_DATABASE_URL`. En GitHub los lanza `ci.yml` con un
servicio `postgres:17`. En local, apunta la variable a una base vacía y de usar y tirar, **nunca a Supabase**: cada
test recrea el esquema `public` (el test lo rechaza si la URL contiene `supabase`).

## Pre-commit

Este repo usa [pre-commit](https://pre-commit.com) para revisar los ficheros **antes de cada commit**.
Si algo falla, el commit se cancela y se muestra el motivo. Lo principal es que impide subir
secretos por error (claves `.pem`, tokens o cadenas de conexión con contraseña).
No es infalible: cubre los patrones habituales, no cualquier contraseña escrita a mano.

### Instalación (una vez por clon)

```bash
pip install pre-commit
pre-commit install
```

A partir de ahí se ejecuta solo con cada `git commit`.

### Qué comprueba

<!-- hooks:start -->
| Hook | Qué hace |
|---|---|
| `trailing-whitespace` | Quita espacios sobrantes al final de las líneas |
| `end-of-file-fixer` | Asegura el salto de línea final |
| `check-yaml` | Valida que los `.yml` (incluido el workflow) sean correctos |
| `check-merge-conflict` | Detecta marcas de conflicto sin resolver |
| `check-added-large-files` | Evita subir ficheros grandes por accidente |
| `detect-private-key` | Bloquea claves privadas (`.pem`) |
| `gitleaks` | Bloquea tokens y claves de formatos conocidos (AWS, GitHub…) |
| `no-db-url-password` | Bloquea URLs de base de datos (postgresql, mysql, mongodb) que incluyan usuario y contraseña. Hook propio, porque `gitleaks` no las detecta |
| `genera-readme` | Regenera las tablas de hooks y secrets de este README |
| `ruff` | Revisa el código Python y corrige lo que puede (`ruff.toml`) |
<!-- hooks:end -->

(Tabla generada por `scripts/gen_readme.py`. Al añadir un hook o secret nuevo, descríbelo en ese script.)

### Uso habitual

```bash
pre-commit run --all-files      # revisar todo el repo a mano
pre-commit autoupdate --freeze  # actualizar los hooks y volver a fijarlos por SHA (sin --freeze se pierde el fijado)
git commit --no-verify          # saltarse los hooks (solo en una emergencia)
```

Algunos hooks arreglan los ficheros por ti. Si un commit falla porque modificaron algo,
haz `git add` de los cambios y vuelve a hacer el commit.

## Qué no se sube nunca

El `.gitignore` excluye `*.pem`, `sessions.json`, `.env` y `__pycache__/`.

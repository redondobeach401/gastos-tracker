# Changelog

Lo que trae cada versión (tag) de enable-banking, de la más reciente a la más antigua. Formato de las versiones:
`MAJOR.MINOR.PATCH` (`feat` sube el MINOR, `fix` el PATCH). No existe la 1.1.0: se pasó de la 1.0.0 a la 1.2.0.

## 1.17.0 · 2026-10-28

Requiere ejecutar `schema.sql` en Supabase (permisos nuevos, vistas solo en EUR y lectura de `saldos_iniciales`).

- **Un banco caído ya no bloquea a los demás.** Antes un error de una cuenta revertía todo lo guardado en la ejecución.
  Ahora cada cuenta se confirma al terminar, se sigue con las demás y se falla al final; el issue de fallo lista las
  cuentas que no se pudieron leer.
- **El saldo se cuadra con el banco.** Tras guardar cada cuenta se compara el saldo contabilizado del banco con
  `saldos_iniciales` más los movimientos y se avisa si difiere en más de `SALDO_TOLERANCIA` (1 €). Si la comprobación
  falla, solo sale un aviso en el log.
- **Moneda real.** Se guarda la moneda de cada movimiento, se avisa si no es EUR y las vistas de Looker solo suman euros.
- **Ventana de 30 días** (`VENTANA_DIAS`) en vez de 7, para ver lo que el banco contabiliza tarde.
- **Copia de seguridad** también de `saldos_iniciales`, que se rellena a mano y no se puede regenerar.
- **Seguridad.**
  - `app_fetcher` solo puede actualizar las columnas que usa; `looker` ya no lee `raw` (el JSON completo del banco).
  - Dependencias actualizadas (`certifi`, `virtualenv`, `filelock`) tras un `pip-audit` con CVE reales.
  - `ci.yml` ejecuta `pip-audit` y `zizmor`, y `auditoria.yml` repite `pip-audit` cada lunes y abre un issue si
    aparece algo.
  - `auth.py` comprueba el `state` de la URL pegada.
- **Robustez.**
  - `concurrency` y `timeout-minutes` en los workflows.
  - `statement_timeout` de 2 minutos en la base.
  - Reintentos en la llamada a Anthropic (429, 5xx y 529).
  - Tope de 100 páginas por cuenta en `fetch_transactions`.
- Refactor: `main()` de `fetch.py` partido en funciones.

## 1.16.0 · 2026-10-28

- **Logs de la IA más claros.** El grupo "Categorización con IA" dice siempre qué ha pasado: que no hay gastos
  pendientes (antes salía en silencio), con qué gastos llama a la API, que la llamada ha fallado y cuántos gastos
  quedan sin categoría, y cuáles ha dejado la IA sin categorizar. El aviso de clave ausente indica que el secret
  está vacío o no existe.
- Documentación: sección de versiones en `CLAUDE.md` y este changelog.

## 1.15.0 · 2026-10-28

- **Viajes por fechas.** Nueva sección `viajes` en `categorias.yml` con las fechas de cada viaje: todo gasto del
  banco en esas fechas pasa a `Viajes` (`category_source = 'viaje'`, columna `viaje` con el nombre), salvo lo
  manual y las categorías de `no_cambian` (alquiler, suscripciones, Bizum...). Se aplica en cada fetch a todo el
  histórico, así que añadir un viaje o corregir sus fechas se arregla solo. Vista `v_gasto_por_viaje` para Looker.
  El historial y la IA no aprenden de estos gastos. 
  *Requiere la migración de `schema.sql`.*
- **Descripciones de categoría para la IA.** Sección `descripciones` en `categorias.yml`: una frase por categoría
  que se incluye en el prompt. Antes, una categoría sin gastos en el historial (como `Tarjeta`) hacía que la IA
  respondiera NO_SE sin avisar. Un test exige describir todas las categorías de gasto.
- **Comer fuera es `Ocio`.** Bares, tabernas, bocaterías y cafés pasan de `Comida` a `Ocio`; `Comida` queda solo
  para supermercados.
- Nueva categoría `Tarjeta` (comisiones de la tarjeta).

## 1.14.0 · 2026-10-27

- Traspasos automáticos entre cuentas propias (`TRASPASO AUTOMATICO DE`) marcados como `is_transfer`.
- Traspasos por pareja de Revolut y Trade Republic con hasta 2 días de diferencia entre las dos cuentas.
- El error de la API del banco incluye el endpoint y el id de la petición.
- Las notas locales de auth y fetch se ignoran en git.
- Documentado el pendiente del saldo de `abanca-1` con los movimientos retenidos.

## 1.13.0 · 2026-10-23

- Logs con resumen final (`Resumen:`), errores de API legibles (estado y mensaje) y aviso (issue) si la IA
  clasifica menos de la mitad de los gastos.
- Documentada la categorización con IA y los logs en el README.

## 1.12.2 · 2026-10-23

- Reglas para Uber Eats y farmacias (`Salud`).
- La categoría `Farmacia` se elimina y queda solo `Salud`.

## 1.12.1 · 2026-10-23

- Categoría `Farmacia` en `categorias.yml` (sustituida en la 1.12.2).

## 1.12.0 · 2026-10-23

- Cuando la IA no sabe categorizar un gasto, el log recomienda qué categoría crear.
- Nueva categoría `Salud`.

## 1.11.0 · 2026-10-23

- **Categorización con IA** (`ia.py`, Claude Haiku) para lo que ni las reglas ni el historial cubren. Marca
  `category_source = 'ia'` para poder revisarlo en Looker y no aprende de sí misma.
- Rendimiento: inserción por lotes, traspasos acotados a la ventana de lectura, JWT cacheado, y el historial no se
  lee si no hay gastos pendientes.
- Regla: `ESPECIALIDADCAFE` como `Comida` (después pasó a `Ocio`, en la 1.15.0).
- Actualización semanal de dependencias.

## 1.10.0 · 2026-10-20

- El workflow `fetch-bank` ejecuta `main` en lugar de la última tag.
- Aviso (issue) con el resumen de gastos sin categoría de todo el histórico, no solo de la ventana de hoy.
- Corregido el fichero del resumen de avisos.
- Anthropic añadido a `Servicios`.
- Convención: los commits no se atribuyen a Claude.

## 1.9.0 · 2026-10-20

- **Saldos reales:** tabla `saldos_iniciales` y vistas `v_saldo_mensual` y `v_saldo_actual`.
- **Vistas SQL para Looker Studio:** `v_gasto_mensual_categoria` y `v_ahorro_mensual`.
- **Vigilante del fetch:** aviso si `fetch-bank` lleva más de 36 horas sin terminar bien (`vigilar-fetch.yml`).
- El workflow ya no tiene `schedule`: lo lanza cron-job.org.
- Workflow manual para probar el bot de Telegram.

## 1.8.0 · 2026-10-20

- Los avisos también salen por **Telegram**, además de como issue.

## 1.7.3 · 2026-10-17

- El log de `fetch.py` separa las fechas corregidas de los recategorizados.

## 1.7.2 · 2026-10-17

- Al relanzar `fetch.py` se corrige la fecha de los movimientos ya guardados.

## 1.7.1 · 2026-10-17

- La fecha del movimiento es `value_date` (la de gasto), no la de anotación del banco.
- El workflow diario se ejecuta en la última tag y a las 23:00 (la tag se sustituye por `main` en la 1.10.0).

## 1.7.0 · 2026-10-16

- `palabras_ignoradas` en `categorias.yml`: lugares y palabras genéricas que el historial no usa para decidir.
- El historial explica sus decisiones y hay un banco de pruebas (`scripts/evaluar_categorizacion.py`).
- Tests de propiedades de la categorización con hypothesis.

## 1.6.0 · 2026-10-16

- **Categorización por historial** (`categorize.py`): aprende qué palabras van con qué categoría en los gastos ya
  categorizados y lo aplica a los que las reglas no cubren.

## 1.5.1 · 2026-10-16

- No avisa de "gasto sin categoría" en los traspasos detectados por pareja.

## 1.5.0 · 2026-10-16

- Lista `categorias_permitidas`: una categoría mal escrita en una regla hace fallar los tests y sugiere la correcta.
- Los hooks de pre-commit se fijan por SHA y Dependabot agrupa las actualizaciones en un PR semanal.

## 1.4.0 · 2026-10-16

- Las reglas de categorización pasan de `fetch.py` a `categorias.yml`: añadir una no exige tocar el código.

## 1.3.0 · 2026-10-16

- `schema.sql` y rol de solo lectura para la copia de seguridad.
- Tests de integración del SQL contra un Postgres real.
- Se ignoran los saltos de línea al final de los secrets de conexión.

## 1.2.0 · 2026-10-16

- **Copia de seguridad semanal cifrada** de `transactions`.
- Avisos por issue de caducidad del consentimiento, fallos y cuentas sin movimientos.
- CI: tests en cada push y PR, pre-commit y gitleaks sobre todo el historial, y Dependabot.
- Tests de categorización, traspasos y normalización.
- No se guarda el saldo posterior en `raw`.

## 1.0.0 · 2026-10-16

- **Primera versión:** descarga diaria de movimientos de Abanca y B100 vía Enable Banking (PSD2) a Supabase.
- Categorización por reglas, con importes positivos como `Ingresos`.
- Recategoriza al relanzar los movimientos sin categoría, muestra nuevos y recategorizados y avisa de gastos sin
  categoría.
- Detección de traspasos entre cuentas propias, por patrón y por pareja en otra cuenta.
- Reglas iniciales: OVpay (`Viajes`), Metro de Madrid (`Transporte`) y entradas (`Eventos/Ocio Puntual`).
- Workflow de GitHub Actions endurecido y con dependencias fijadas por hash, pre-commit con las tablas del README
  regeneradas, y `CLAUDE.md` con la convención de commits.

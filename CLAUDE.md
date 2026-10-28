# Instrucciones del proyecto

## Commits

Los mensajes de commit siguen [Conventional Commits](https://www.conventionalcommits.org/es/v1.0.0/):

```
<tipo>(<ámbito opcional>): <descripción en minúscula, en imperativo, sin punto final>

<cuerpo opcional: qué y por qué>
```

Tipos: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`.

- Un commit por cambio lógico. No mezclar, por ejemplo, formato de código con una funcionalidad nueva.
- Descripción en español, de 72 caracteres como máximo.
- No añadir líneas `Co-Authored-By` ni otra atribución a Claude en los commits.
- Si rompe compatibilidad, añadir `!` tras el tipo (`feat!: ...`) y explicarlo en el cuerpo.

Ejemplos:

```
feat: marca traspasos entre cuentas propias con is_transfer
fix: evita duplicados al relanzar fetch.py
ci: cambia la hora del workflow diario
chore: añade pre-commit y README
```

## Seguridad

- Nunca subir `*.pem`, `sessions.json`, `.env` ni contraseñas o URLs de base de datos.
- Los hooks de pre-commit lo comprueban; no saltárselos con `--no-verify`.


## Contexto

Tracker de gastos y ahorros de mis cuentas de Abanca y B100. Todo debe ser gratis.

- Fetcher en Python (`fetch.py`), ejecutado a diario con GitHub Actions (lo lanza cron-job.org por `workflow_dispatch`, el workflow no tiene `schedule`), que lee las cuentas vía Enable Banking (PSD2).
- Los avisos (consentimiento a punto de caducar, cuenta sin movimientos, fallo del workflow) salen como issue de GitHub y por Telegram.
- Base de datos Postgres en Supabase. El histórico del antiguo Google Sheet ya está importado.
- Dashboard en Looker Studio conectado a Supabase (lee la tabla `transactions` con el rol `looker`).
- Cuentas: Abanca (abanca-1), B100 Tarjeta (abanca-2), B100 Save (abanca-3), B100 Health (abanca-4).
- Desarrollo en Windows con VS Code.

## Versiones

Cada cambio relevante se etiqueta con una tag semántica (`MAJOR.MINOR.PATCH`: `feat` sube el MINOR, `fix` el PATCH) y se anota en [CHANGELOG.md](CHANGELOG.md), que lista lo hecho en cada tag. Al crear una tag, añade su entrada al changelog en el mismo commit.

## Mejoras

Hechas:

- Aviso antes de que caduque el consentimiento PSD2 (14 días de margen), por issue y Telegram.
- Detección de traspasos entre cuentas propias (`is_transfer`, por patrón y por pareja).
- Upsert por `(account, external_id)`, sin duplicados al relanzar `fetch.py`.
- Categorización automática, por capas: reglas en `categorias.yml` → viajes por fechas → historial (`categorize.py`) → IA (`ia.py`, Claude Haiku, `category_source = 'ia'`, revisable en Looker; necesita el secret `ANTHROPIC_API_KEY`). Medida con gastos reales del banco: ~77 % de acierto, ~90 % sin contar el viaje a Ámsterdam.
- Descripciones por categoría (`descripciones` en `categorias.yml`): le dicen a la IA qué significa cada una. Una categoría nueva sin gastos en el historial necesita la suya, o la IA responde NO_SE; un test lo exige. Criterio: `Comida` es solo supermercado, comer fuera es `Ocio`.
- Logs: `logs.py` (anotaciones, grupos plegables y tarjeta de resumen de Actions), línea `Resumen:` al final de `fetch.py`, errores de API con estado y mensaje, y aviso (issue) si la IA clasifica menos de la mitad. El grupo "Categorización con IA" dice siempre qué ha pasado: sin pendientes, con qué gastos llama a la API, si falla, y qué gastos deja sin categorizar.
- Vistas SQL para Looker Studio en `schema.sql` (`v_gasto_mensual_categoria`, `v_ahorro_mensual`, `v_saldo_mensual`, `v_gasto_por_viaje`).
- Viajes por fechas (`viajes` en `categorias.yml`, `viajes.py`): gastos en las fechas de un viaje → `Viajes` con `category_source = 'viaje'` y columna `viaje`, salvo `no_cambian` y lo manual. Madrid y León son casa, no viaje.
- Aviso si `fetch-bank` no termina bien en 36 h (`vigilar-fetch.yml`), por si cron-job.org deja de lanzarlo.
- Un banco caído no bloquea a los demás: cada cuenta se confirma (commit) al terminar, se sigue con las demás y se falla al final; el issue de fallo lista las cuentas que no se pudieron leer.
- Saldo contra el banco: tras guardar cada cuenta, `fetch.py` compara el saldo contabilizado del banco (`CLBD`, por API) con `saldos_iniciales` + movimientos y avisa (issue y Telegram) si difiere en más de `SALDO_TOLERANCIA` (1 €). Va en un savepoint: si falla, solo avisa.
- Moneda real: se guarda la moneda de cada movimiento, se avisa si no es EUR y las vistas de Looker solo suman euros. Ventana de relectura de 30 días (`VENTANA_DIAS`).
- Seguridad: `app_fetcher` solo actualiza las columnas que usa; `looker` lee `transactions` por columnas y sin `raw`; `pip-audit` y `zizmor` en CI y `auditoria.yml` semanal; `auth.py` comprueba el `state`.
- Robustez: `concurrency` y `timeout-minutes` en los workflows, `statement_timeout` de 2 min, reintentos en la llamada a Anthropic y tope de páginas por cuenta.
- Copia de seguridad cifrada de `transactions` y de `saldos_iniciales` (esta última se rellena a mano y no se puede regenerar).
- Revisar y corregir categorías: `scripts/revisar_ia.py` lista lo que ha puesto la IA (o el historial) y `scripts/corregir.py <categoría> <id>...` lo corrige como `manual`, sin pasar por el SQL Editor. Lo manual no lo tocan las reglas, los viajes ni la IA, y de ello aprenden el historial y la IA.

Pendiente:

- Mover Looker de la tabla a las vistas (en Looker, a mano). `looker` ya no puede leer `raw`: una fuente con `select *` sobre `transactions` falla.
- Saldos reales: `saldos_iniciales` calibrada el 2026-10-20 con los saldos del banco; si se desvían de la app, falta algún movimiento. Desde la 1.17.0 `fetch.py` lo comprueba solo contra el saldo del banco (API) y avisa si la diferencia supera 1 €.
- Verificar que la copia de seguridad se puede restaurar (nadie ha probado nunca una restauración): en `backup.yml`, antes de subir el artifact, descifrar los `.csv.gpg` con `BACKUP_PASSPHRASE`, cargarlos con `\copy` en un `postgres:17` desechable con `schema.sql` aplicado y comparar los recuentos con los que imprime `backup.py`. Si falla, no se sube una copia mala. Aparte, sigue sin haber copia fuera de GitHub (los artifacts duran 90 días).

Aparcadas a propósito (analizadas con datos reales; retomar solo si hay síntomas o lo pido):

- Nombres de cuenta estables en un `cuentas.yml` (hoy `abanca-N` depende del orden que devuelve el banco en `auth.py`).

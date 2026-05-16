# Deuda técnica resuelta: alineación globex de EMA/ADX diaria

**Fecha:** 2026-05-16
**Estado:** Patch implementado bajo feature flag `GLOBEX_ALIGNED` (default `False` para no afectar LIVE)
**Recomendación:** flippear el default a `True` tras validación adicional con más historia

## Problema

`calcular_ema_diaria` y `calcular_adx_ayer` en `estrategias.py` usaban `resample('D')` con el índice en zona horaria ET, agrupando barras entre **00:00 ET y 00:00 ET**. La sesión real CME del MES corre de **18:00 ET (apertura globex) hasta 17:00 ET del día siguiente (daily settlement)**. El bucket diario partía el overnight asiático/europeo en dos días distintos, contaminando los cálculos de tendencia.

## Implementación

Nuevo flag en `config.py`:

```python
import os as _os
GLOBEX_ALIGNED = _os.environ.get('GLOBEX_ALIGNED', '0') == '1'
```

Helper en `estrategias.py`:

```python
def _resample_daily(df_sub):
    if not GLOBEX_ALIGNED:
        return df_sub.resample('D')
    df_s = df_sub.copy()
    df_s.index = df_s.index - pd.Timedelta(hours=18)
    return df_s.resample('D')
```

`calcular_ema_diaria` y `calcular_adx_ayer` ahora delegan a `_resample_daily`. **Default = comportamiento histórico**. Live no setea la env var, queda intacto.

## Validación 1 — Cuantificación del desfase (50 días de cache 2026-03-06 a 2026-05-15)

| Indicador | Días divergentes | Comentario |
|---|---|---|
| Sesgo EMA(20) (LONG vs SHORT) | **1/50 (2%)** | Bajo impacto. La EMA es lenta. |
| Filtro ADX_MIN=20 | **10/50 (20%)** | Alto impacto. Y sistemático: ADX globex es siempre menor (media −5.9 puntos). |

El filtro ADX actual es **más permisivo de lo que corresponde**: deja pasar 10 días donde la tendencia real de la sesión CME no supera el umbral.

## Validación 2 — Backtest comparativo (variante B-ventana15, 200 evals, seed 1-200)

| Métrica | OFF (actual) | ON (globex) | Delta |
|---|---:|---:|---:|
| Pass rate | 99.0% | **100.0%** | +1.0 pp |
| Explosiones | 0.0% | 0.0% | — |
| Timeouts (no llega a target) | 1.0% | 0.0% | −1.0 pp |
| Mediana días para pasar | 33 | **27** | −6 días (−18%) |
| Media días para pasar | 31.4 | 28.1 | −10% |
| Trades/eval (mediana) | 14 | 11 | −21% |
| Win rate | 46.9% | **60.8%** | +13.9 pp |
| Profit factor | 4.75 | **7.42** | +56% |
| Max DD media | $238 | **$172** | −28% |
| Max DD p95 | $495 | **$345** | −30% |
| Max DD peor caso | $523 | $505 | −3% |
| TP / SL / Timeout totales | 1349 / 242 / 1283 | 1359 / 119 / 757 | TPs ≈ iguales, SLs y timeouts ≈ mitad |

**Lectura clave:** el patch toma casi exactamente los mismos TPs (1349 → 1359, +0.7%) pero **filtra la mitad de los SLs y la mitad de los timeouts**. Es decir, el ADX globex-aligned separa correctamente "trades que van a perder" de "trades que van a ganar". Lo que esperás de un buen filtro de tendencia.

## Validación 3 — Sin lookahead

Test de regresión: para 3 cortes temporales aleatorios, comparar EMA/ADX calculadas con `df[df.index <= t]` vs `df[df.index <= t + 2h]` cuando ambos cortes caen en el mismo bucket globex:

```
t=2026-05-06 21:00 ET (mismo bucket): ema delta=0.000000, adx delta=0.000000 ✓
t=2026-04-29 04:00 ET (mismo bucket): ema delta=0.000000, adx delta=0.000000 ✓
t=2026-05-04 11:00 ET (mismo bucket): ema delta=0.000000, adx delta=0.000000 ✓
```

El shift `-18h` no introduce leak — los buckets pasados no cambian cuando se agrega data nueva al mismo bucket abierto.

## Limitaciones

1. **Período corto:** solo 50 días de cache (≈10 semanas). El hallazgo de "ADX globex sistemáticamente menor" podría ser específico del régimen RTH-volátil / overnight-tranquilo que dominó marzo-mayo 2026. Validar con más historia (90-180 días) antes de quitar el feature flag.
2. **Daily maintenance break (17:00-18:00 ET):** ignorado. En la práctica esa hora no tiene barras, así que el bucket de 24h con shift -18h queda efectivamente 23h. Ruido despreciable para EMA/ADX.
3. **Solo se testeó ORB.** ICT usa el mismo `calcular_ema_diaria` (línea 467 de estrategias.py: `signal_actividad_minima`), así que el flag aplica automáticamente. Sería razonable re-correr el backtest comparativo de ICT antes de activarlo en LIVE para ICT.

## Decisión recomendada

**Activar gradualmente:**

1. **Ya hecho:** patch en repo, flag default `False` → live sigue idéntico.
2. **Esta semana:** correr backtest con cache de 180 días (descargar histórico extra) para confirmar que el delta no es ruido del período.
3. **Si se confirma:** flippear `GLOBEX_ALIGNED` default a `True` en `config.py` (o setear `GLOBEX_ALIGNED=1` como env var en `.github/workflows/bot_diario.yml`).
4. **Rollback:** si algo raro pasa en live, setear `GLOBEX_ALIGNED=0` (o quitar la env var) — vuelta inmediata al comportamiento histórico sin redeploy.

## Archivos modificados

- `config.py` — agrega flag `GLOBEX_ALIGNED` con override por env var
- `estrategias.py` — agrega `_resample_daily()` helper, usado por `calcular_ema_diaria` y `calcular_adx_ayer`

## JSONs de backtest

- `research/results_orb_b_ventana15_200evals_OFF.json` (resultado actual)
- `research/results_orb_b_ventana15_200evals_ON.json`  (con patch)

Generados con seeds 1-200 sobre `research/cache_mes_1h.csv`.

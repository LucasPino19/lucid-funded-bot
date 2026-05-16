# Hallazgo: el sesgo SHORT > LONG es PARCIALMENTE artefacto del bug de alineación ADX

**Fecha:** 2026-05-16
**Estado:** análisis cerrado (limitado por período corto)
**Recomendación:** no actuar todavía, re-validar con más historia

## Observación original

> "Las 3 estrategias muestran WR consistentemente más alto en SHORT que en LONG
>  (ORB: 31% LONG vs 45% SHORT). Puede ser sesgo del período de 50 días o algo
>  estructural."

## Resultado del análisis

Sobre **2200+ trades** distribuidos en 200 evals Monte Carlo (cache 2026-03-06 → 2026-05-15, variante B-ventana15):

### Antes del patch (GLOBEX_ALIGNED=False, comportamiento actual)

| Dirección | n | WR | TP/SL/TO | avg P&L | total P&L |
|---|---:|---:|---:|---:|---:|
| LONG | 1263 | **35.1%** | 443 / 12 / **808** | $+91 | $+115K |
| SHORT | 1611 | **56.2%** | 906 / 230 / 475 | $+102 | $+164K |
| **Gap** | | **+21pp** | | | |

### Después del patch (GLOBEX_ALIGNED=True)

| Dirección | n | WR | TP/SL/TO | avg P&L | total P&L |
|---|---:|---:|---:|---:|---:|
| LONG | 977 | **57.0%** | 557 / 12 / 408 | $+117 | $+114K |
| SHORT | 1258 | **63.8%** | 802 / 107 / 349 | $+134 | $+169K |
| **Gap** | | **+6.8pp** | | | |

## Interpretación

**El gap SHORT > LONG cae de +21pp a +6.8pp con el patch globex.** Es decir, **dos tercios del sesgo eran espurios**, causados por el ADX mal alineado.

### Mecanismo

Mirando timeouts (= trade que no llega ni a SL ni a TP antes del cierre):

- LONG OFF: **808 timeouts de 1263 (64%)** — el bot entraba pero el precio no llegaba
- LONG ON:  408 timeouts de 977 (42%) — el filtro globex eliminó 50% de los timeouts LONG
- SHORT OFF: 475 / 1611 (29%) — mucho menos timeouts
- SHORT ON:  349 / 1258 (27%) — apenas cambia

**El ADX inflado afectaba mucho más a los LONG.** Probablemente porque en bull moderado, el overnight asiático/europeo cargaba momentum alcista que entraba "como tendencia" en el ADX 00:00-00:00 ET. Cuando RTH abría, el precio ya había gastado mucho de ese movimiento, los breakouts LONG entraban con condiciones que parecían tendencia pero ya no lo eran, y se quedaban hasta EOD sin llegar al TP.

Con el ADX alineado a la sesión CME real (18:00 → 17:00 ET), ese "fantasma de tendencia" desaparece. Los LONG que pasan el filtro son los que tienen tendencia real intra-sesión, no la huella del overnight.

### El gap residual de +6.8pp

Tres explicaciones posibles, no distinguibles con 50 días:

1. **Estructural.** S&P en RTH: los SHORT breakouts requieren más convicción para activarse, y cuando se activan suelen ser más decisivos. Los LONG breakouts son más comunes pero más "tibios". Esto se ve en literatura intraday y es consistente con el período bull moderado.
2. **Régimen específico.** En el período cache (+8.3% en 2 meses, trend score 0.16, 55% días verdes), la **contra-tendencia** es informativa: cuando aparece un SHORT setup en bull moderado, suele ser una reversión real. Los LONG en bull moderado son ruido.
3. **Ruido de sample.** Con 30 trades únicos por dirección (deduplicados), el intervalo de confianza al 95% del WR es ~±18pp. El gap de 6.8pp está dentro del ruido estadístico.

## Caracterización del período (contexto)

- Rango: $6367 → $7537 (range 18.4%)
- Inicio → fin: +8.3% en 70 días calendario (~20% anualizado, bull moderado)
- 55% días verdes (32/58)
- Retorno diario medio +0.17%, std 1.04% → trend score 0.16 (positivo pero débil)

Un período típicamente "bullish-quiet". No es un mercado lateral ni una caída. **El resultado podría no extrapolarse a otros regímenes.**

## ¿Qué hacer al respecto?

**Nada por ahora.** Razones:

1. El patch GLOBEX_ALIGNED ya recomienda re-validar con 180+ días. Ese mismo análisis confirma o refuta el gap residual.
2. Con 6.8pp y sample chico, no vale aplicar mitigaciones direccionales (ej. "subir el filtro para LONG"). Riesgo alto de overfit al período.
3. La operativa real LIVE ya tiene 0% explosiones / 100% pass en ambos backtest. No hay urgencia.

Cuando se tenga más historia (180+ días, idealmente cubriendo bear/chop/bull), repetir el análisis. Si el gap residual se mantiene > 5pp después de filtros, ahí sí considerar:
- Filtro asimétrico de ADX (más estricto para LONG)
- Filtro de "open en relación al high/low overnight"
- O simplemente operar SHORT-only en regímenes específicos

## Distribución por hora y tamaños

(Trades únicos, OFF)

| | LONG | SHORT |
|---|---|---|
| Hora 11 ET | 7 | 7 |
| Hora 12 ET | 4 | 3 |
| Hora 13 ET | 4 | 4 |
| Hora 14 ET | 2 | 3 |
| ORB size mediano | 32.2 | 26.5 |
| ADX mediano | 31.3 | 25.8 |

Distribución horaria casi idéntica. LONGs requieren mayor ORB y mayor ADX para activarse — coherente con que están operando sobre setups "más ruidosos" en bull market.

## Archivo de análisis

[`outputs/sesgo_dir.py`](computer:///outputs) (script reproducible)

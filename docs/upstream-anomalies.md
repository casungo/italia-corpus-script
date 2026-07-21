# Anomalie upstream Normattiva

Verifica del 21 luglio 2026 sui nomi dei file restituiti dalle collezioni ufficiali
Normattiva. Queste anomalie non vengono risolte scegliendo il primo documento: tutte le identità
coinvolte restano escluse finché il codice redazionale ufficiale non è univoco.

## XML troncati

La collezione `Regi decreti` ha restituito 11 payload lunghi esattamente 1 MiB. Non sono XML
completi e restano nella quarantena del dry-run. I codici coinvolti sono: `091U0260`, `008U0150`,
`011U1413`, `026U0596`, `029U0062`, `030U1629`, `013U0453`, `034U0383`, `030U1643`,
`040U1077` e `065U2641`.

## Collisioni dei codici redazionali

In ciascuna riga entrambi i file ufficiali dichiarano lo stesso `eli:id_local`, ma le directory e
le URN identificano atti diversi. Il codice non è quindi utilizzabile come chiave canonica senza
una correzione upstream.

| Codice | Prima URN | Seconda URN |
| --- | --- | --- |
| `099G0224` | `urn:nir:stato:decreto.legislativo:1999-05-11;152` | `urn:nir:stato:decreto.legislativo:1999-05-22;196` |
| `094G0140` | `urn:nir:stato:decreto.del.presidente.della.repubblica:1994-02-11;242` | `urn:nir:stato:decreto.legge:1994-02-18;110` |
| `093G0018` | `urn:nir:stato:decreto.legislativo:1992-12-30;534` | `urn:nir:stato:decreto.legge:1994-01-07;9` |
| `093G0125` | `urn:nir:stato:decreto.legge:1994-02-14;106` | `urn:nir:stato:decreto.legge:1993-03-19;69` |
| `095G0089` | `urn:nir:stato:decreto.legge:1996-02-26;76` | `urn:nir:stato:decreto.legge:1995-02-28;57` |
| `092G0023` | `urn:nir:stato:decreto.legislativo:1992-12-30;539` | `urn:nir:stato:legge:1992-01-07;19` |
| `094G0139` | `urn:nir:stato:decreto.del.presidente.della.repubblica:1994-02-11;241` | `urn:nir:stato:legge:1994-02-14;124` |
| `088G0373` | `urn:nir:stato:decreto.legge:1988-07-30;303` | `urn:nir:stato:legge:1988-07-25;318` |
| `090G0219` | `urn:nir:stato:legge:1991-06-06;177` | `urn:nir:stato:legge:1990-06-23;181` |
| `081U0307` | `urn:nir:stato:regio.decreto:1881-07-07;307` | `urn:nir:stato:legge:1981-05-25;307` |
| `092G0443` | `urn:nir:ministero.industria.commercio.e.artigianato:decreto:1992-05-07;400` | `urn:nir:stato:decreto.del.presidente.della.repubblica:1991-11-08;442` |
| `000G0047` | `urn:nir:stato:legge:2000-01-27;16` | `urn:nir:stato:decreto.legislativo:2000-12-28;443` |
| `046U0182` | `urn:nir:stato:decreto.del.capo.provvisorio.dello.stato:1946-08-23;182` | `urn:nir:stato:decreto.legislativo.luogotenenziale:1946-02-22;182` |
| `081U0453` | `urn:nir:stato:regio.decreto:1881-10-02;453` | `urn:nir:stato:legge:1981-08-05;453` |
| `092G0051` | `urn:nir:stato:decreto.legge:1993-01-26;19` | `urn:nir:stato:decreto.del.presidente.della.repubblica:1991-11-08;441` |
| `063U1471` | `urn:nir:stato:regio.decreto:1863-09-20;1471` | `urn:nir:stato:decreto.del.presidente.della.repubblica:1963-10-11;1471` |

# Anomalie upstream Normattiva

Verifica aggiornata al 27 luglio 2026 sui file restituiti dalle collezioni ufficiali Normattiva.

## XML troncati

La collezione `Regi decreti` ha restituito 72 payload lunghi esattamente 1 MiB. Non sono XML
completi e restano in quarantena. L'eccezione scade il 31 agosto 2026 ed è valida solo se classe,
collezione, messaggio e conteggio corrispondono esattamente.

## Collisioni dei codici redazionali

Il corpus completo dimostra che `eli:id_local` non è una chiave globale: migliaia di codici
identificano URN diverse, anche per atti separati da un secolo. La URN è quindi la chiave
canonica; quando un codice è ambiguo entrambi gli atti vengono conservati con un suffisso di
percorso stabile e l'indice del codice contiene tutte le corrispondenze.

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

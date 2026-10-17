# Risques et impacts amplifiés — uTest Machinery

**Objet :** suivre les **risques** (événements à prévenir) et les **impacts constatés en revue** (RETEX), leurs causes possibles, les extrapolations systémiques et les actions préventives proposées. Les deux suivis coexistent sur cette même page sans confondre risques potentiels et anomalies de PR. Ce registre **consigne les orientations acceptées** lors de la revue du 17 octobre 2026 ; **il ne prétend pas que les mitigations sont déjà implémentées**. Les décisions techniques détaillées devront, si elles sont retenues, être intégrées ultérieurement aux [spécifications](../architecture/architecture.md), au [plan de développement](../plan/implementation.md) et aux critères de qualification.

**Notation indicative :** probabilité P et impact I de 1 (faible) à 5 (très élevé), score **S = P × I** ; `20–25` critique, `12–19` important, `1–11` moindre. Cette cotation sert à ordonner les discussions et ne représente pas une fréquence mesurée. **Statuts :** `À traiter` (mitigation à définir ou décider), `À étudier` (choix technique ouvert), `À mettre en œuvre` (plan validé, application non démontrée), `En attente de confirmation` (application documentée, efficacité à revoir), `Maîtrisé` (efficacité confirmée par preuves), `Accepté` (limitation assumée), `Différé` (renforcement non prioritaire). **Le registre est mis à jour exclusivement lors de la revue indépendante des risques, pas par l'agent de développement.** L'agent documente ses choix et preuves dans Beads ; l'examen du code et des PR permet de décider d'un éventuel changement de statut ou de cotation.

## Registre consolidé

| ID — risque | P / I / S | Mitigation retenue ou proposée | Statut | Actions / suivi (aucune réalisation présumée) |
| --- | --- | --- | --- | --- |
| **R01 — SDD incomplète, ambiguë ou silencieuse** | 5 / 4 / **20** | SDD **source de vérité par défaut** ; continuer avec les hypothèses les plus raisonnables, clairement distinctes des exigences. Attendus `SPECIFIED`, `ASSUMED`, `UNDETERMINED`. Détecter aussi erreurs non décrites et code sans justification explicite. | À traiter | PR génération/rapport : expliciter hypothèses, provenance, tests associés et clarification proposée. Vérifier les trois statuts sur des SDD lacunaires. |
| **R02 — Écart hôte/MCU** | 5 / 2 / **10** | **Limitation acceptée** : vérifier la logique C accessible sur PC, sans prétendre qualifier les propriétés temporelles ou physiques de la cible. Ne pas construire de classement complexe de « fidélité MCU ». | Accepté | Conserver un avertissement simple dans le rapport. QEMU/Renode possibles plus tard ; aucune émulation matérielle dans ce scope. |
| **R03 — Code non compilable/testable sur PC** | 5 / 4 / **20** | Mocks/stubs/adaptations privés pour les **dépendances**, pas pour le comportement testé. `READY/ADAPTED/BLOCKED` par unité ; continuer avec les unités éligibles. Une ISR, une calibration ou un driver peut être **partiellement** testé pour sa logique, même si ses propriétés physiques/temps réel restent hors scope. | À traiter | Définir et éprouver des critères courts : partie réelle de la logique exécutée, oracle observable, substitutions maîtrisées, effort raisonnable, absence de simulation MCU. Tester ADC, ISR, calibration et cas SDK bloqué. |
| **R04 — Oracles/test cases s'alignant sur le code plutôt que la SDD** | 4 / 5 / **20** | Interpréter la SDD en priorité ; relier chaque attendu à une exigence ou une hypothèse déclarée. Ne jamais réparer un échec en adaptant silencieusement l'attendu au code observé. | À traiter | Qualifier sur des défauts connus masqués au générateur ; inspecter tests et provenance des oracles. |
| **R05 — Illusion de conformité** | 4 / 5 / **20** | Séparer résultats d'exécution, couverture structurelle, couverture SDD, robustesse des tests et limites. Une synthèse critique LLM commente **des preuves vérifiables**, sans les inventer. | À traiter | Définir les limites visibles dans l'unique rapport HTML et vérifier qu'un test PASS ne conduit pas à une conclusion excessive. |
| **R06 — Fragilité de Claude Code/CCR/DeepSeek** | 3 / 4 / **12** | Utiliser le LLM **ponctuellement** pour des tâches ciblées du DAG, pas un agent unique actif durant toute la campagne. Entrées limitées, skills versionnées, sorties vérifiées, itérations à étapes avec checkpoint. | À traiter | Tester appels in-container, formats invalides et reprise après un appel manqué ; mesurer latence/tokens par invocation. |
| **R07 — Mocks non représentatifs ou trop favorables** | 4 / 4 / **16** | Documenter les interfaces et hypothèses accessibles, les arguments, retours, erreurs, séquences et état des mocks ; garder le vrai traitement logiciel. Ajouter commentaires aux tests et documentation synthétique générée de la suite. | À traiter | Sur fixture capteur, vérifier appels, erreurs, séquences et traçabilité mock ↔ test ↔ SDD ; signaler les contrats non documentés dans les inputs. |
| **R08 — Incompatibilités Ceedling/LLVM/Mull et autres outils** | 4 / 4 / **16** | Pinner une chaîne de versions compatibles, profils séparés, essayer l'assemblage réel sur un petit module avant d'étendre. Ne pas réimplémenter ces moteurs. | À traiter | Démonstrateur natif Ceedling + couverture + sanitizers + Mull ; conserver les limitations vérifiées. |
| **R09 — Couverture MC/DC incomplète ou mal interprétée** | 3 / 3 / **9** | Lire les mesures **natives** LLVM, décisions exclues et dénominateurs réels, sans convertir la métrique en garantie de conformité. | À traiter | Comparer les vecteurs booléens attendus et les décisions réellement instrumentées. |
| **R10 — Mutation/fuzzing coûteux mais peu discriminants** | 3 / 4 / **12** | Mutants pertinents, scénarios/propriétés rattachés à la SDD et corpus réutilisable. Conserver survivants, mutants non valides et résultats non conclusifs. | À étudier | Mesurer les défauts réellement révélés sur fixtures ; ajuster l'applicabilité plutôt que lancer tous les profils partout. |
| **R11 — Matrice de traçabilité fausse ou opaque** | 4 / 5 / **20** | **Un seul modèle structuré**, références contrôlées déterministiquement, matrice SDD ↔ code ↔ scénarios ↔ tests ↔ exécutions ↔ preuves. Le LLM évalue la **pertinence**, non le calcul des liens ou pourcentages. Un seul rapport HTML avec plusieurs vues. | À traiter | Évaluer les capacités simples d'outils gratuits (ex. StrictDoc/Doorstop) avant d'écrire du custom ; choisir la voie la plus fiable, sans imposer de changement aux SDD ni une seconde base concurrente. |
| **R12 — Maintenance altérant une suite auparavant validée** | 4 / 4 / **16** | Tests incrémentaux, empreintes et oracles protégés ; **health check** du banc via compilabilité, témoins négatifs/mutation ciblée et cohérence SDD/test. | À étudier | Décider quand déclencher le health check (création/changement/rerun), comment afficher ses preuves et s'il doit être une option de `verify`, **sans nouvelle commande par défaut**. |
| **R13 — Campagne non reproductible** | 3 / 4 / **12** | Conserver hashes des inputs, tests, dépendances, toolchain, profils, résultats ; refuser silencieusement la réexécution sur un état différent. | À traiter | Vérifier le rejeu après reconstruction du conteneur et une modification volontaire des sources. |
| **R14 — CI rapide mais peu probante** | 3 / 4 / **12** | CI PR **minimale et rapide** ; tous les tests lourds pertinents exécutés **localement avant la PR**, avec commandes, résultats et preuves vérifiables. | À traiter | À chaque PR : vérifier la présence et la cohérence des preuves locales, refuser les gates non démontrées même si CI verte. |
| **R15 — Diagnostics et corrections LLM erronés** | 4 / 4 / **16** | Skills ciblées et versionnées (SDD, plan, génération, critique de tests, interprétation, diagnostic d'erreurs), sorties structurées, causes/localisations/preuves vérifiables ; propositions non appliquées automatiquement au code. | À traiter | Évaluer un petit ensemble de corrections attendues, avec liens vérifiés et revue critique. |
| **R16 — DAG opaque, fragile ou irrécupérable** | 3 / 4 / **12** | Dagu supervise avec journal/statuts déterministes et reprise ; **aucun LLM de surveillance permanent**. Diagnostic LLM à la demande lors de blocages difficiles, sur logs pertinents et bornés. | À traiter | Simuler erreur de configuration, outil cassé, interruption et reprise ; exiger un diagnostic exploitable via CLI, sans masquer les erreurs sous SUCCESS. |
| **R17 — Durée d'exécution et tokens trop élevés** | 5 / 4 / **20** | Mesurer durées, tokens et retries **par étape**, réutiliser les résultats inchangés, campagnes pertinentes seulement. **Budgets indicatifs** plutôt que quotas bloquants ; vraies limites de sécurité contre boucles infinies, fuzz indéfini et retries sans fin. | À étudier | Définir estimation de durée libFuzzer **dans le plan** selon objectif/corpus, point d'arrêt justifié et diagnostics ; éviter des dizaines de réglages projet. Mesurer consommation réelle avant optimisation. |
| **R18 — Confidentialité des données/tokens** | 2 / 3 / **6** | Infrastructure privée reconnue ; conserver seulement les précautions élémentaires : credentials hors Git/images/logs et accès autorisé. Pas de système de gouvernance supplémentaire. | Accepté | Vérification de base des secrets dans les PR et l'image ; réévaluer uniquement si le contexte d'hébergement change. |
| **R19 — Code C à risque dans Docker** | 2 / 4 / **8** | Utilisateurs de confiance, précautions Docker minimales ; ne pas retarder la valeur fonctionnelle par un projet de sandbox avancé. | Différé | Mentionner le risque résiduel ; renforcer l'isolation si usage multi-utilisateur ou sources non fiables. |
| **R20 — Surcomplexité/glue custom** | 5 / 5 / **25** | Conserver l'autonomie de l'agent pour les décisions courantes. Avant un ajout technique non trivial, envisager outils natifs, intégrations plus simples ou reformulation du besoin, et consigner brièvement choix, alternatives et coût de maintenance dans **Beads**. Faire remonter les dérives disproportionnées avant de modifier l'architecture ou le scope. | **À mettre en œuvre** | **Plan validé** : inscrire la consigne dans `CLAUDE.md`, laisser l'agent justifier les choix significatifs dans Beads (référencé par la PR) ; **revue indépendante** de l'efficacité après plusieurs PR. Aucun score réduit avant preuve. |
| **R21 — Mauvais appariement SDD ↔ code** | 4 / 5 / **20** | Inventaire déterministe C et références SDD ; rapprochement sémantique proposé par LLM, liens/identifiants vérifiés par code. Rapporter exigences sans implémentation et code sans justification SDD ; ne pas inventer d'interface de test inexistante. | À traiter | Qualifier cas multi-fonctions/multi-exigences et comportements absents ; décider test possible vs écart sans test ; inspecter les correspondances incorrectes. |
| **R22 — Suite de tests artificiellement « bonne »** | 4 / 5 / **20** | Montrer à l'utilisateur que les tests compilent, exercent les comportements prévus et détectent des défauts connus (témoins ou mutations). Comparer à un jugement indépendant, sans prétendre à l'autocertification complète. | À étudier | Health check **léger, intégré au workflow existant**, réexécutable après évolution des tests ; afficher ce qu'il prouve et ses limites. Définir un niveau comparable à une bonne suite conçue par un développeur expérimenté sur fixtures. |
| **R23 — Workflow imposant toute la chaîne, sans étapes inspectables** | 4 / 4 / **16** | Permettre l'exécution **étape par étape** (analyse et plan → génération/inspection tests → vérification → interprétation/rapport) et un **mode enchaîné automatique** réutilisant les mêmes étapes, artefacts et checks. | À étudier | Vérifier ce que l'actuelle CLI permet déjà ; proposer l'extension minimale de `plan/generate/verify/interpret` sans prolifération de commandes. Tester arrêt/reprise entre étapes, validation humaine facultative et mode complet. |

## Arbitrages confirmés et points à éclaircir

- **Source de vérité :** la SDD guide les tests, même avec des manques. `SPECIFIED` = directement exigé ; `ASSUMED` = hypothèse explicitement motivée ; `UNDETERMINED` = attendu non déductible. La confiance d'une hypothèse ne doit pas se transformer en preuve de conformité.
- **Éligibilité native :** `READY/ADAPTED/BLOCKED` suffit ; la décision est prise à l'échelle la plus fine raisonnable. Une ISR, une calibration ou un pilote de registre peut être **testé partiellement** : paramètres, algorithmes, calculs, erreurs, séquences et transitions. Restent hors preuve native les mesures analogiques réelles, le timing et la concurrence matérielle. Ne pas construire un simulateur de MCU.
- **Traçabilité :** matrice **déterministe** et HTML clair, dans le rapport unique, avec commentaires de pertinence LLM séparés. Arbitrage encore ouvert entre un outil libre prêt à l'emploi et un adaptateur minimal ; **aucune infrastructure de requirements management additionnelle n'est décidée**.
- **Health check :** ambition de vérifier la sensibilité et la pertinence de la suite, pas de prétendre prouver toutes ses qualités. Mutation/témoins indépendants sont privilégiés, sans exiger une seconde suite complète ou une commande dédiée.
- **Orchestration :** les phases doivent pouvoir être inspectées et lancées séparément, puis enchaînées sans duplication lorsqu'on privilégie l'autonomie. Le mode de découpage exact de la CLI reste à étudier ; **aucune nouvelle commande n'est décidée ici**.
- **LLM et coûts :** tâches ponctuelles avec skills versionnées et sorties validées ; supervision de premier niveau déterministe. Instrumenter temps, tokens (quand disponibles) et retries. Éviter les budgets arbitraires bloquants, mais conserver un arrêt pour traitement bloqué/infini.
- **Sécurité :** R18 et R19 ne justifient pas de complexifier le produit à ce stade, sans supprimer les précautions élémentaires.

## Suivi des actions lors des revues

Le statut d'un risque ne passe à `En attente de confirmation` qu'après une application observable, puis à `Maîtrisé` seulement si une **revue indépendante** confirme son efficacité sur des preuves (code, tests, tickets Beads et PR). **Seul le responsable de la revue des risques** ajuste cette page et les scores ; Claude Code ne la modifie pas. La colonne « Actions / suivi » doit rester concise : décision, preuve/référence éventuelle et prochain examen, sans recopier les tickets Beads. Le [modèle de PR](../../.github/PULL_REQUEST_TEMPLATE.md) référence les tickets Beads pour retrouver les justifications.

**État initial :** arbitrages et mitigations consignés ; **aucune mitigation technique n'est déclarée implémentée dans cette mise à jour**. Les scores doivent être réévalués après les premiers résultats sur du C/SDD réel, les coûts mesurés et les revues d'efficacité de suites générées.

---

## RETEX 01 — Impacts identifiés lors de la revue de la PR #5

**Source :** [PR #5 — première feature, bootstrap et contrôle](https://github.com/aradenac/utest-machinery/pull/5), revue du 17 octobre 2026. Les constats proviennent de l'inspection du code, des tests et des preuves GitHub. **Ils n'impliquent pas que les défaillances aient eu lieu en production, ni que tous les scénarios aient été reproduits dans WSL.** La revue a intercepté ces défauts et risques de défaut *avant fusion*. La colonne « Cause probable » présente une **hypothèse à confirmer** et non une cause racine démontrée.

Les actions de correction de la **PR #5** sont suivies **dans cette PR** et dans Beads, **pas dans ce RETEX**. Ici, les actions AP sont exclusivement des **préventions pour les futures features**. Leur inscription dans ce registre est validée, **mais aucune n'est encore décidée pour mise en œuvre ni exécutée**.

### Matrice des impacts et généralisation

| ID | Impact identifié en revue | Cause probable (hypothèse) | Généralisation de la cause et conséquences possibles | Actions préventives liées | Suivi |
| --- | --- | --- | --- | --- | --- |
| **IMP-01** | CLI dépendante de Dagu installé sur l'hôte, malgré l'autonomie Docker prévue. | Composants testés isolément, sans reproduire le parcours utilisateur complet. | **Systémique probable :** d'autres intégrations (LLM, compilateur C, couverture, mutation) peuvent être vertes individuellement mais inutilisables ensemble ; produit livré incomplet malgré des tests réussis. | [AP-01](#ap-01--preuve-par-critère-dacceptation), [AP-02](#ap-02--qualification-dans-lenvironnement-réel) | Impact identifié ; prévention à décider. |
| **IMP-02** | La consultation de \`mtm status\` peut remplacer le dernier diagnostic persistant. | Tests de commandes isolées, sans vérification des séquences et transitions d'état. | **Systémique probable :** perte ou altération d'historique, checkpoints, plans, tests approuvés ou preuves lors de consultations, réinitialisations et reprises ; fiabilité d'audit compromise. | [AP-01](#ap-01--preuve-par-critère-dacceptation), [AP-03](#ap-03--tests-dinvariants-et-de-cycle-de-vie) | Impact identifié ; prévention à décider. |
| **IMP-03** | Diagnostics de workflow incomplets dans la sortie utilisateur et les enregistrements. | Vérification du code de retour sans contrôle de tous les champs du contrat fonctionnel. | **Systémique probable :** une fonctionnalité pourrait être considérée achevée sur une preuve partielle ; erreurs inexpliquées et rétablissement coûteux pour d'autres étapes du workflow. | [AP-01](#ap-01--preuve-par-critère-dacceptation), [AP-03](#ap-03--tests-dinvariants-et-de-cycle-de-vie) | Impact identifié ; prévention à décider. |
| **IMP-04** | \`init --force\` peut faire perdre des sélections de périmètre existantes. | Invariant de préservation des données non couvert lors d'une opération de réinitialisation. | **Systémique probable :** pertes silencieuses de scénarios, oracles validés, états et preuves lors des évolutions incrémentales ou migrations. | [AP-03](#ap-03--tests-dinvariants-et-de-cycle-de-vie) | Impact identifié ; prévention à décider. |
| **IMP-05** | Écriture de fichiers potentiellement root-owned ou changement récursif de propriétaire sur le banc monté. | Vérification côté conteneur sans examen systématique des effets sur le système hôte. | **Systémique probable :** permissions, chemins, volumes ou variables d'environnement supposés présents ; fonctionnement limité au poste de développement, effets indésirables sur des sources utilisateur. | [AP-02](#ap-02--qualification-dans-lenvironnement-réel) | Impact potentiel identifié dans le code ; prévention à décider. |
| **IMP-06** | Une CLI installée depuis un wheel peut ne plus disposer de ses schémas de validation. | Tests exécutés dans le checkout source et non sur l'artefact installé indépendamment. | **Systémique probable :** omission de schémas, scripts, modèles, ressources ou chemins requis ; CI réussie mais installation distribuée défaillante. | [AP-02](#ap-02--qualification-dans-lenvironnement-réel) | Impact potentiel identifié dans le code ; prévention à décider. |
| **IMP-07** | Un identifiant de scope invalide au regard du schéma peut être écrit. | Validation en lecture/chargement, mais pas de garantie équivalente avant chaque écriture. | **Systémique probable :** création progressive d'états incompatibles, impossibles à relire, maintenir ou migrer ; erreurs tardives difficiles à diagnostiquer. | [AP-03](#ap-03--tests-dinvariants-et-de-cycle-de-vie) | Impact identifié ; prévention à décider. |
| **IMP-08** | Répartition des justifications et historiques entre \`worklog/\`, Beads et PR insuffisamment explicite. | Finalité de chaque support documentaire non formalisée. | **Structurel et évitable :** duplication des informations, sources de vérité concurrentes et coût accru de maintenance/tokenisation. **Le \`worklog\` lui-même est utile** comme mémoire persistante de l'agent ; le problème est l'absence de séparation claire. | [AP-04](#ap-04--mémoire-de-travail-et-instructions-à-jour) | Clarification de gouvernance à décider. |
| **IMP-09** | La branche de feature ne contient pas toutes les dernières règles de \`CLAUDE.md\` de \`main\`. | Branche longue et instructions évoluant parallèlement, sans réconciliation finale systématique. | **Récurrent possible :** futurs agents appliquant des politiques obsolètes, omettant des contraintes récentes ou livrant des PR divergentes ; le décalage peut résulter du calendrier, sans faute intrinsèque de l'agent. | [AP-04](#ap-04--mémoire-de-travail-et-instructions-à-jour) | Impact identifié ; prévention à décider. |
| **IMP-10** | Détails de route LLM interne et de profil hôte consignés dans un dépôt GitHub public. | Transcription fidèle de l'environnement réel sans distinction suffisante entre documentation utile et information destinée à publication. | **Conditionnel et généralisable :** propagation de topologies internes, chemins, données d'environnement ou configurations sensibles dans les traces versionnées ; **aucune fuite de token n'a été établie par cette revue**. | [AP-05](#ap-05--vérification-légère-avant-publication) | Exposition documentaire identifiée ; prévention à décider. |

### Actions préventives systémiques proposées

Ces **cinq propositions ont été acceptées pour inscription et discussion**. **Statut initial commun : \`À décider\`** ; ni modification d'instructions, ni ajout de tests, ni modification de CI ou du plan de développement dans cette PR documentaire. Éviter de créer de nouveaux outils, sous-agents ou procédures systématiques coûteuses pour les appliquer.

#### AP-01 — Preuve par critère d'acceptation

**Cible :** IMP-01, IMP-02 et IMP-03 ; couvre également la faiblesse transversale des « CI vertes mais comportement utilisateur non prouvé » (R14).

**Action concrète envisagée :** compléter \`CLAUDE.md\` et les conditions de passage du [plan de développement](../plan/implementation.md) : **avant ouverture d'une PR**, l'agent associe chaque critère d'acceptation à une commande/test effectivement exécuté et à une preuve, ou le marque explicitement non démontré, dans le **ticket Beads de la feature**. La PR référence le ticket et les résultats, sans reproduire la matrice.

**Vérification future :** aucune capacité annoncée comme terminée sans preuve appropriée ; les critères non prouvés restent visibles lors de la revue.

**Statut :** À décider — aucune mise en œuvre.

#### AP-02 — Qualification dans l'environnement réel

**Cible :** IMP-01, IMP-05 et IMP-06 ; en lien avec R13 et R14.

**Action concrète envisagée :** exiger **un essai d'intégration local représentatif lorsqu'une feature traverse un outil ou une frontière d'environnement** : vraie commande publique, artefact installé si pertinent (wheel/image), dépendances minimales sur l'hôte, volumes et permissions. Ne pas imposer ce coût à chaque test unitaire ; enregistrer la commande et le résultat dans Beads/PR.

**Vérification future :** démontrer que l'usage promis fonctionne depuis l'interface et l'environnement prévus, sans dépendance cachée au checkout ou au poste de développement.

**Statut :** À décider — aucune mise en œuvre.

#### AP-03 — Tests d'invariants et de cycle de vie

**Cible :** IMP-02, IMP-03, IMP-04 et IMP-07 ; en lien avec R12, R13 et R16.

**Action concrète envisagée :** ajouter à \`CLAUDE.md\` une règle : lorsqu'une feature **crée, lit, modifie, restaure ou supprime** un état, identifier les invariants critiques et tester des **séquences d'usage** (écrire → relire, échouer → consulter, interrompre → reprendre, modifier → conserver l'historique). Valider les schémas aux frontières d'écriture **et** de lecture, sans framework dédié.

**Vérification future :** chaque feature manipulant un état persistant possède au moins les tests de transition nécessaires à ses invariants principaux, y compris les effets indésirables après échec.

**Statut :** À décider — aucune mise en œuvre.

#### AP-04 — Mémoire de travail et instructions à jour

**Cible :** IMP-08 et IMP-09 ; en lien avec R20.

**Action concrète envisagée :** préciser dans \`CLAUDE.md\` : **\`worklog/\` = mémoire détaillée et chronologique de l'agent** (recherches, tentatives, erreurs, observations et hypothèses) ; **Beads = synthèse des décisions finales, alternatives et preuves de revue** ; **PR = livrables, résultats et références Beads** ; **\`docs/\` = référentiel normatif produit**. Avant soumission d'une PR, confronter la branche aux changements de règles applicables intervenus sur \`main\` depuis sa création, sans imposer un second backlog.

**Vérification future :** décisions retrouvables via Beads, cheminement reconstructible via \`worklog/\`, aucune règle récente applicable oubliée.

**Statut :** À décider — aucune mise en œuvre.

#### AP-05 — Vérification légère avant publication

**Cible :** IMP-10 ; en lien avec R18, tout en conservant son acceptation du risque résiduel.

**Action concrète envisagée :** ajouter une consigne simple de prépublication dans \`CLAUDE.md\` : examiner les nouveaux fichiers versionnés pour repérer secrets, identifiants personnels, chemins privés et détails d'infrastructure non nécessaires au produit ; préférer les variables configurables et exemples assainis. Réutiliser les contrôles existants, **sans nouvel outil de gouvernance**.

**Vérification future :** pas de divulgation nouvelle, accidentelle ou inutile, dans les PR futures.

**Statut :** À décider — aucune mise en œuvre.

### Revue et mesure d'efficacité

Les impacts **IMP-01 à IMP-10** sont des événements ou défauts *identifiés lors de la revue* ; ils ne sont pas « corrigés » simplement parce qu'une action préventive est proposée. Les hypothèses de cause sont à confirmer à partir des preuves \`worklog/\`, Beads et des prochaines campagnes de qualification. **La correction ponctuelle de la PR #5 n'est pas suivie dans cette section.**

À une prochaine revue, suivre trois indicateurs qualitatifs : **(1)** réapparition des mêmes familles de défauts, **(2)** détection autonome par l'agent avant PR, **(3)** charge des nouveaux contrôles au regard de leur valeur. Ne déclarer une action « appliquée » qu'avec une preuve ; ne conclure à l'efficacité qu'après plusieurs PR. L'analyse et la mise à jour de cette page restent indépendantes de l'agent de développement.

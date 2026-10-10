# gr2 glossary

The words gr2 uses for its own commands, each with the command that does it. This file is generated from [vocabulary.json](vocabulary.json) by `scripts/gen_vocabulary_glossary.py`; edit the JSON and regenerate, not this file.

## lane

A unit of work with its own checkout. Create it, enter it to work, exit it to put it down, remove it to end it.

Command: `gr2 lane create`, `gr2 lane enter`, `gr2 lane exit`, `gr2 lane remove`, `gr2 lane list`.

`lane remove` refuses while the lane holds work found nowhere else: uncommitted changes, a stash, an unpushed commit, or anything in its checkout that is not one of its repos. `lane list` marks each lane removable or keep.

## pin

Record an exact commit. The workspace pins each repo; `review pin` pins the heads under review.

Command: `gr2 review pin`.

`review bind` is the earlier name and still works.

## stamp

An approval of one exact pin. If the work changes, the pin moves and the old stamp no longer matches.

Command: `gr2 review stamp`.

A stamp records an approval. Approver names are self-declared and the record is unsigned. `review approve` is the earlier name and still works.

## repin

Pin again. Changed work is pinned again with `review pin`; when the target base moves, `review repin` rebases a frozen range onto it.

Command: `gr2 review repin`.

`review repin` writes a new frozen directory and refuses if the range would change. `review rebind` is the earlier name and still works.

## gate

The merge gate: `review merge` refuses unless the pinned work is unchanged, required checks pass and enough stamps match it.

Command: `gr2 review merge`.

How many stamps are enough is a setting. With none required, the stamp count is skipped.

## review

A reader checks one exact pin before the work is merged; `review open` rebuilds it in a lane to read and run.

Command: `gr2 review`.

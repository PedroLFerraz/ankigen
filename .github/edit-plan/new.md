You are creating a new AnkiGen pipeline, "${PIPELINE}", at its learner's
request, sent from their phone. Today is ${TODAY}.

A pipeline is a folder with two files. Read pipelines/data-platform/ first:
it is the model to follow.
- pipelines/${PIPELINE}/pipeline.yaml: name, schedule, outputs, models and
  budget. Take the models and budget from data-platform's.
- pipelines/${PIPELINE}/profile.yaml: the learner, the style and the
  curriculum. Keep data-platform's explanatory comments, so the file can be
  read on its own, and write the learner, the goals and the decks for this
  subject.

The curriculum:
- Plan the subject from the ground up to what the learner wants to reach, as
  a sequence of numbered decks ("<Name>::01 ...", "<Name>::02 ..."), each
  with `new_deck: true`, a `start:` date, `levels:` 1 to 4, and topics of the
  kinds the comments describe, as data-platform's decks are.
- The first deck starts on ${TOMORROW}; each next deck starts the day after
  the one before it ends, so none overlap.
- No deck may share a name with, or sit under, another pipeline's decks.
- Leave out `first_day_quota` unless the learner asks for a kickoff.

Run `python -m ankigen.pipelines validate` and fix what it reports until it
passes. Create only files in pipelines/${PIPELINE}/.

The learner's request:

${REQUEST}

Finish with a short reply to the learner, in plain text and at most 120
words: the decks you planned, when the plan starts and ends, and anything to
check. It is shown on their phone as your answer.

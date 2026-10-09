You are changing the study plan of the AnkiGen pipeline "${PIPELINE}", at its
learner's request, sent from their phone. Today is ${TODAY}.

The plan is pipelines/${PIPELINE}/profile.yaml. Read it first: its comments
explain decks, start dates, levels, topic kinds and quotas. pipeline.yaml
beside it holds the schedule, models and outputs.

The next run writes curriculum day ${NEXT_DAY}. Days before it are already
written: leave their topics, and the start dates of decks that began before
it, as they are. Change what comes after.

Rules:
- Change only files in pipelines/${PIPELINE}/. Touch pipeline.yaml only when
  the request is about the schedule, the models or the outputs.
- Edit in place. Keep the file's comments, order and style; do not rewrite
  the whole file.
- Keep the decks in sequence. When a deck grows, shrinks or moves, move the
  `start:` of the decks after it so none overlap, unless the request asks
  for that.
- A new deck follows the shape and naming of the decks already there.
- Run `python -m ankigen.pipelines validate` and fix what it reports until
  it passes.
- If the request is a question, answer it and change nothing.

The request:

${REQUEST}

Finish with a short reply to the learner, in plain text and at most 120
words: what you changed, with deck names and dates, and anything you did not
do and why. It is shown on their phone as your answer.

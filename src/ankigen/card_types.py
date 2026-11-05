"""Card type definitions — fields, templates, CSS, and stable model IDs for genanki."""

BASIC = {
    "name": "basic",
    "model_id": 1607392320,
    "fields": ["Question", "Answer"],
    "template_front": "{{Question}}",
    "template_back": "{{FrontSide}}<hr id='answer'>{{Answer}}",
    "css": """.card {
  font-family: arial;
  font-size: 20px;
  text-align: center;
  color: black;
  background-color: white;
}""",
}

DETAILED = {
    "name": "detailed",
    "model_id": 1607392321,
    "fields": ["Question", "Summary", "Explanation", "Image", "Reference"],
    "template_front": '<div class="front-question">{{Question}}</div>',
    "template_back": """<div class="detailed-back">
  <div class="question">{{Question}}</div>
  <hr>
  <div class="summary">{{Summary}}</div>
  <div class="explanation">{{Explanation}}</div>
  {{#Image}}<div class="image">{{Image}}</div>{{/Image}}
  {{#Reference}}<div class="ref"><a href="{{Reference}}">Ref</a></div>{{/Reference}}
</div>""",
    "css": """.card {
  font-family: arial;
  font-size: 20px;
  text-align: center;
  color: #e0e0e0;
  background-color: #1a1a2e;
}
.front-question {
  font-size: 22px;
  color: #ffffff;
  padding: 20px;
}
.detailed-back .question {
  font-size: 18px;
  color: #aaaaaa;
  margin-bottom: 8px;
}
.detailed-back hr {
  border: 1px solid #3a3a5e;
  margin: 10px 0;
}
.detailed-back .summary {
  font-size: 20px;
  font-weight: bold;
  color: #4fc3f7;
  margin: 12px 0;
}
.detailed-back .explanation {
  font-size: 16px;
  color: #cccccc;
  text-align: left;
  line-height: 1.5;
  margin: 12px 10px;
}
.detailed-back .image {
  margin: 15px auto;
}
.detailed-back .image img {
  max-width: 100%;
  max-height: 400px;
  border-radius: 6px;
}
.detailed-back .ref {
  margin-top: 12px;
  font-size: 14px;
}
.detailed-back .ref a {
  color: #4fc3f7;
  text-decoration: none;
}""",
}

VISUAL = {
    "name": "visual",
    "model_id": 1607392322,
    "fields": ["Image", "Title", "Explanation"],
    "template_front": '<div class="visual-front">{{Image}}</div>',
    "template_back": """<div class="visual-back">
  <div class="visual-image">{{Image}}</div>
  <hr>
  <div class="title">{{Title}}</div>
  <div class="explanation">{{Explanation}}</div>
</div>""",
    "css": """.card {
  font-family: arial;
  font-size: 20px;
  text-align: center;
  color: #e0e0e0;
  background-color: #1a1a2e;
}
.visual-front {
  padding: 20px;
}
.visual-front img {
  max-width: 100%;
  max-height: 500px;
  border-radius: 8px;
}
.visual-back .visual-image {
  margin-bottom: 10px;
}
.visual-back .visual-image img {
  max-width: 80%;
  max-height: 300px;
  border-radius: 6px;
  opacity: 0.7;
}
.visual-back hr {
  border: 1px solid #3a3a5e;
  margin: 10px 0;
}
.visual-back .title {
  font-size: 22px;
  font-weight: bold;
  color: #4fc3f7;
  margin: 12px 0;
}
.visual-back .explanation {
  font-size: 16px;
  color: #cccccc;
  text-align: left;
  line-height: 1.5;
  margin: 12px 10px;
}""",
}

CLOZE = {
    "name": "cloze",
    "model_id": 1607392323,
    "model_type": 1,  # genanki cloze model type
    "fields": ["Text", "Extra"],
    "template_front": "{{cloze:Text}}",
    "template_back": "{{cloze:Text}}<br>{{Extra}}",
    "css": """.card {
  font-family: arial;
  font-size: 20px;
  text-align: center;
  color: #e0e0e0;
  background-color: #1a1a2e;
  line-height: 1.6;
}
.cloze {
  font-weight: bold;
  color: #4fc3f7;
}""",
}

_CODE_CSS = """
code {
  font-family: Menlo, Consolas, monospace;
  background: rgba(127, 143, 166, .18);
  padding: 1px 5px;
  border-radius: 4px;
}
pre {
  text-align: left;
  display: inline-block;
  max-width: 100%;
  overflow-x: auto;
  background: rgba(127, 143, 166, .12);
  padding: 10px 14px;
  border-radius: 6px;
  font-size: 16px;
  line-height: 1.45;
}
pre code { background: none; padding: 0; }"""

# A command or a shortcut, learned both ways: the task asks for the command,
# and the command asks what it does, which is how you meet one in someone
# else's script. One note, two cards.
COMMAND = {
    "name": "command",
    "model_id": 1607392324,
    "fields": ["Task", "Command", "Note"],
    "templates": [
        ("Task to command",
         '<div class="task">{{Task}}</div>',
         '<div class="task">{{Task}}</div><hr id="answer">'
         '<div class="command">{{Command}}</div>'
         '{{#Note}}<div class="note">{{Note}}</div>{{/Note}}'),
        ("Command to task",
         '<div class="command">{{Command}}</div><div class="ask">What does it do?</div>',
         '<div class="command">{{Command}}</div><hr id="answer">'
         '<div class="task">{{Task}}</div>'
         '{{#Note}}<div class="note">{{Note}}</div>{{/Note}}'),
    ],
    "css": """.card {
  font-family: arial;
  font-size: 20px;
  text-align: center;
  color: #e0e0e0;
  background-color: #1a1a2e;
  line-height: 1.5;
}
.command { font-size: 24px; margin: 8px 0; }
.ask, .note { font-size: 15px; color: #9aa5b8; margin-top: 10px; }""" + _CODE_CSS,
}
COMMAND["template_front"], COMMAND["template_back"] = COMMAND["templates"][0][1:]
CLOZE["css"] += _CODE_CSS
BASIC["css"] += _CODE_CSS
DETAILED["css"] += _CODE_CSS

CARD_TYPES = {
    "basic": BASIC,
    "command": COMMAND,
    "detailed": DETAILED,
    "visual": VISUAL,
    "cloze": CLOZE,
}


def get_card_type(name: str) -> dict | None:
    """A card type's definition by name, or None for a name with none."""
    return CARD_TYPES.get(name)

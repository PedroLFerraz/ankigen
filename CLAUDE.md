# ankigen

## Workflow

- Every change starts from a GitHub issue. If there isn't one, draft it with the `new-issue` skill and file it before writing code.
- One branch and one PR per issue. The PR body starts with `Closes #N` and follows `.github/pull_request_template.md`, including how the change was tested.
- `claude[bot]` reviews every PR. Work through its findings with the `address-review` skill: fix or rebut each one and reply on its thread before merging.
- Behavior changes come with tests.
- Leave a `TODO:` in code only for work worth an issue: once it's merged to `master`, it becomes one.

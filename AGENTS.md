# TrailPrint repository workflow

These instructions apply to every change made in this repository.

## Approval gates

1. Begin with read-only investigation and remain in planning until a
   decision-complete plan has been presented to the user.
2. Do not edit repository files until the user explicitly approves
   implementation.
3. After implementation and validation, report the results and wait for the
   user's explicit approval before committing, pushing, opening or merging a
   pull request.

## Starting implementation

1. Check out `main` and run `git pull --ff-only`.
2. Inspect the working tree. Preserve unrelated user changes and generated
   files; never discard, overwrite, stash, or include them without permission.
3. Create a descriptively named `feature/<slug>` branch from the updated
   `main` branch before editing files.
4. Keep all implementation changes on that feature branch. Stage and commit
   only files that belong to the approved task. Never force-push.

If unrelated changes prevent a safe branch switch or overlap the requested
work, stop and ask the user how to proceed.

## Validation

- Run relevant focused tests and the complete automated test suite.
- For changes involving Blender, terrain, water, STL generation or validation,
  or Bambu 3MF generation or validation, perform an end-to-end build using the
  checked-in `tests/fixtures/gpx/hawaii-test.gpx` file. Use a disposable test
  project outside tracked production project directories so validation cannot
  overwrite user deliverables.
- Inspect the actual generated files and validation report. Do not infer
  success solely from parameters, mocks, manifests, or unit tests.
- For 3MF changes, generate and validate a real 3MF from the GPX build and
  confirm that it opens successfully in Bambu Studio.
- When a change reproduces a route-specific problem, also validate with the
  relevant checked-in GPX fixture. Add a sanitized regression fixture under
  `tests/fixtures/gpx/` when no suitable fixture exists.
- Treat failures caused by Blender, Bambu Studio, map services, or other
  external dependencies as blockers; do not report them as successful
  validation.
- Documentation, analytics, and unrelated UI-only changes do not require the
  real-GPX build unless they affect generation or export behavior.

## Publishing an approved change

Only after the user explicitly approves the validated implementation:

1. Commit only the approved, task-scoped files.
2. Push the feature branch and create a pull request.
3. Merge the pull request with a merge commit.
4. Delete the remote feature branch.
5. Check out `main`, run `git pull --ff-only`, and delete the merged local
   feature branch with `git branch -d`.
6. Verify that `main` matches `origin/main` and report any preserved local
   modifications.

Never force-delete an unmerged branch. Never commit credentials, unrelated
generated output, or local configuration.

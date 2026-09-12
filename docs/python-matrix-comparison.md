# Python matrix comparison

The default main Python concurrency remains two. Manual `ci` dispatch accepts
`python-parallelism=2` or `3` and passes the number through the main component
workflow to both raw Python component rows and qualified Python rows. The
component planning boundary rejects any other value before starting producers.
Toolchain and GCC matrix concurrency, compiler jobs, BuildKit worker controls,
qualification scope and candidate defaults retain their existing settings.

GitHub provides string-valued dispatch choices, numeric reusable-workflow
inputs and a matrix `max-parallel` setting; see the [official workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).
The scheduling setting is recorded as an observation with source commit,
run/attempt and event in `python-scheduling-<run>-<attempt>`. It does not grant
artifact reuse or assert a performance improvement.

After the component workflow has passed its actual main acceptance, run the
baseline and comparison sequentially on the same main commit:

```sh
gh workflow run ci.yml --ref main -f python-parallelism=2
# Wait for the entire run, preserve its run ID and artifacts, then repeat.
gh workflow run ci.yml --ref main -f python-parallelism=3
```

Do not dispatch a batch simultaneously: the existing concurrency groups can
queue or replace pending runs. Complete at least three matching-input baseline
runs and the affected-change validation from the implementation plan. Compare
the selected work and authenticated input/component identities before treating
runs as comparable. Keep build, report reuse, requalification and fresh final
integration counts separate. Record queue time, first reliable feedback,
critical-path wall time, job-hours, component transfers and repeated compiler
work, alongside each job's resource observation. A matching source commit alone
does not prove equal runner or qualification conditions.

Only measured main results can justify changing the default. Local regression
coverage proves the parameter route and rejection behavior, not a speedup.

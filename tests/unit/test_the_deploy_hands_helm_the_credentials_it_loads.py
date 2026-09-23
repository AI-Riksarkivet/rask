"""`make k3s-up` loads `.env` and the `hf auth login` token so that `helm upgrade` can pass them on.

That only works if both happen in ONE shell. make runs every LOGICAL recipe line in its own shell, and
a logical line ends at the first physical line without a trailing backslash — so a block of `: #`
comment lines placed between the loader and the upgrade silently splits them. The loader then exports
`HF_TOKEN` / `AWS_*` into a shell that exits, and the upgrade's `${HF_TOKEN:+--set-string ...}`
expands to nothing. Measured 2026-09-23 with `make -n k3s-up`: the upgrade ran in the fourth of eight
shells, the loader in the first.

READ OFF `make -n`, not the Makefile text: the question is how MAKE splits the recipe, and only make
answers it. `-o` marks the prerequisites as up to date so nothing but this recipe is printed.
"""

from __future__ import annotations

from tests.unit.k3s_recipe import k3s_up_upgrade_shell as _upgrade_shell


def test_the_upgrade_runs_in_the_shell_that_sourced_dotenv() -> None:
    shell = _upgrade_shell()

    assert ". ./.env" in shell, (
        "`helm upgrade` runs in a different shell from the one that sources .env, so HF_TOKEN and the "
        "AWS_* keys it exports never reach `--set-string secrets.hfToken` / `minio.*Key`. Keep the loader "
        "and the upgrade in one logical recipe line — no un-continued line (a `: #` comment included) between them."
    )


def test_the_upgrade_runs_in_the_shell_that_read_the_hf_token_file() -> None:
    shell = _upgrade_shell()

    assert "huggingface}/token" in shell, (
        "`helm upgrade` runs in a different shell from the one that reads the `hf auth login` token file, so "
        "the gated-model token never reaches the chart and Serve 401s on the first gated download."
    )

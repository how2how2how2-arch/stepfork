"""Stepfork command-line interface."""

from typing import Annotated

import typer
from rich.console import Console

from stepfork import __version__
from stepfork.cli.diff import diff_command
from stepfork.cli.export import export_command
from stepfork.cli.fork import fork_command
from stepfork.cli.inspect import inspect_command
from stepfork.cli.minimize import minimize_command
from stepfork.cli.replay import replay_command
from stepfork.cli.score import score_command
from stepfork.cli.validate import validate_command

app = typer.Typer(
    name="stepfork",
    help="Behavioral regression testing for AI agents.",
    invoke_without_command=True,
)

console = Console()

app.command("diff")(diff_command)
app.command("export")(export_command)
app.command("fork")(fork_command)
app.command("inspect")(inspect_command)
app.command("minimize")(minimize_command)
app.command("replay")(replay_command)
app.command("score")(score_command)
app.command("validate")(validate_command)


def version_callback(value: bool) -> None:
    """Print the Stepfork version and exit."""
    if value:
        console.print(f"stepfork {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            "-V",
            callback=version_callback,
            is_eager=True,
            help="Show the Stepfork version and exit.",
        ),
    ] = None,
) -> None:
    """Turn failed AI-agent runs into reproducible regression tests."""
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()

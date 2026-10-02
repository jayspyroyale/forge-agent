"""The terminal approver: shows a permission request and asks the user."""

from rich.markup import escape
from rich.prompt import Prompt

from forge.cli.output import console, short_json
from forge.security.permissions import ApprovalChoice, Approver, PermissionRequest

_CHOICES = {"y": ApprovalChoice.ALLOW_ONCE, "a": ApprovalChoice.ALLOW_SESSION, "n": ApprovalChoice.DENY}


def make_cli_approver(auto_approve: bool = False) -> Approver:
    """An approver that shows the tool, risk, and arguments, then asks y / a / n."""

    def approve(request: PermissionRequest) -> ApprovalChoice:
        risk = request.risk
        console.print(
            f"[bold yellow]  ? Permission needed[/bold yellow]: [bold]{escape(request.tool_name)}[/bold] "
            f"[yellow]({risk.level})[/yellow]"
        )
        for reason in risk.reasons:
            console.print(f"    [yellow]! {escape(reason)}[/yellow]")
        console.print(f"    [dim]{escape(short_json(request.arguments, limit=500))}[/dim]")
        if auto_approve:
            console.print("    [green]approved (--yes)[/green]")
            return ApprovalChoice.ALLOW_ONCE
        answer = Prompt.ask(
            "    Allow? [bold]y[/bold]es once, [bold]a[/bold]lways this session, [bold]n[/bold]o",
            choices=list(_CHOICES),
            default="n",
            console=console,
        )
        return _CHOICES[answer]

    return approve

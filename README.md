# health-cli
Run some checks periodically locally on your machine. Provide an interface for the status of the checks. Status inteface checks timestamps and is moderately robust.

This is AI-generated and unreviewed code... for now. Also very young so liabe to change

## Motivation
Why is there nothing that does this already? I don't want to use docker or some onine services

## Alternatives and prior work
There are tools like monit and sentry. `cron` sends emails when things fail.

## Installation
pipx install healthcli

## Usage
Set up a job
`hccli add --every 1m curl website`

In cron job or systemd job: `hccli run`

To check run: `hccli`. I have this run in a [plasma-applet-commandoutput](https://github.com/Zren/plasma-applet-commandoutput) KDE widget which I set up with my tool kde-panel.

If you have a systemd timer  you can use `hccli add --every 25h --sdtimer backup-home` to ensure that the timer runs every 25h.

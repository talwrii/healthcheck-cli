# health-cli
Run some checks periodically locally on your machine. Provide an interface for the status of the checks. Status inteface checks timestamps and is moderately robust.

This is AI-generated and unreviewed code... for now. Also very young so liabe to change

## Motivation
Why is there nothing that does this already.

## Installation
pipx install healthcli

## Usage
hccli add --every 1m curl website


In cron job:

hccli run


To check

hccli




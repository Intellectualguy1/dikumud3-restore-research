# Research branch: restore cases on DikuMUD3

This branch of a fork of [Seifert69/DikuMUD3](https://github.com/Seifert69/DikuMUD3) is for research only.
It is the upstream commit `de26ada15b60df1e6f49d712152946745a07fdfc` plus these files, and nothing else:

- `.github/workflows/restore-cases.yml`: builds that commit with the same steps as upstream's CMake workflow,
  then runs `research/restore_cases.py` against a disposable server on the runner.
- `research/restore_cases.py`: telnet driver. Builds one damaged game state (a zone name wrong in
  `vme/etc/zonelist` while a player logs in, as in upstream PR #225), then runs two recovery cases from fresh
  copies of it: restoring the zone alone, and restoring one player's inventory file from a backup.
  A new character is a guest and `quit` drops a guest without writing any file, so the driver types `save`
  for each new character before it takes the backup.

No game source, CMake or script file is changed; the workflow's first step fails the run if any file outside this
list differs from the pinned commit. Players are disposable (Papi, Alice, Bob) and use throwaway passwords.
Nothing here is sent to the upstream repository.

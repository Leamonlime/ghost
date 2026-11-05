-- Session 99b: the Sidequests bucket is gone; fold its 11 historical momentum rows into 'unsorted'.
-- Original rows exported to ~/backups/ghost-postgres/momentum_events_sidequests-20261103.csv.
UPDATE momentum_events SET bucket='unsorted' WHERE bucket='Sidequests' AND id IN (2,3,22,23,24,25,26,27,35,38,39);

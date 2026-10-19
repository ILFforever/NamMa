-- No more rescue tickets or responder role (2026-10-19). NamMa sends no rescue request on anyone's behalf: a danger
-- message gets the DDPM 1784 hotline and what to tell them (agent/triage.py). The web app dropped its staff page the
-- same day. Tickets held PDPA-sensitive health data, so they are deleted, not archived.
DROP TABLE IF EXISTS ticket_events;
DROP TABLE IF EXISTS rescue_tickets;
ALTER TABLE users DROP COLUMN IF EXISTS role;

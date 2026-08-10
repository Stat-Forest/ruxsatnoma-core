-- Foreign keys that cross a file boundary backwards.
--
-- Files apply in name order, so a table cannot reference one defined later.
-- geo.occupancy is created in 30-geo.sql but points at app.application and
-- permit.permit, which appear in 50-app.sql and 70-permit.sql. Those columns are
-- declared as plain uuid there and get their constraints here, once every table
-- exists.

ALTER TABLE geo.occupancy
    ADD CONSTRAINT occupancy_application_fk
    FOREIGN KEY (application_id) REFERENCES app.application (id);

ALTER TABLE geo.occupancy
    ADD CONSTRAINT occupancy_permit_fk
    FOREIGN KEY (permit_id) REFERENCES permit.permit (id);

SELECT
  [AppointmentID],
  [PracticeID],
  CASE
    WHEN (AppointmentStatus IS NOT NULL) THEN
      CASE
        WHEN (NOT (AppointmentStatus IN (@valid_status_codes_0, @valid_status_codes_1, @valid_status_codes_2, @valid_status_codes_3, @valid_status_codes_4))) THEN 'fail'
        WHEN NOT (NOT (AppointmentStatus IN (@valid_status_codes_0, @valid_status_codes_1, @valid_status_codes_2, @valid_status_codes_3, @valid_status_codes_4))) THEN 'pass'
        ELSE 'indeterminate'
      END
    ELSE 'indeterminate'
  END AS tri_state,
  [AppointmentStatus],
  [ScheduleDate]
FROM dbo.Appointments
WHERE (IsDeleted = 0)
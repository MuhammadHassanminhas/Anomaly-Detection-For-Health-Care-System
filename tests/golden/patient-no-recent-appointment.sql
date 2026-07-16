SELECT
  [ProfileID],
  [PracticeID],
  CASE
    WHEN (EnrollmentDate IS NOT NULL) THEN
      CASE
        WHEN (((IsHighCare = 1) OR (IsCarePlus = 1)) AND NOT EXISTS (SELECT 1 FROM dbo.Appointments WHERE (dbo.Appointments.PatientID = dbo.Patient.ProfileID) AND (dbo.Appointments.IsDeleted = 0 AND dbo.Appointments.ScheduleDate >= DATEADD(day, -@recall_window_days, sysdatetime())))) THEN 'fail'
        WHEN NOT (((IsHighCare = 1) OR (IsCarePlus = 1)) AND NOT EXISTS (SELECT 1 FROM dbo.Appointments WHERE (dbo.Appointments.PatientID = dbo.Patient.ProfileID) AND (dbo.Appointments.IsDeleted = 0 AND dbo.Appointments.ScheduleDate >= DATEADD(day, -@recall_window_days, sysdatetime())))) THEN 'pass'
        ELSE 'indeterminate'
      END
    ELSE 'indeterminate'
  END AS tri_state,
  [FirstName],
  [FamilyName],
  [IsHighCare],
  [IsCarePlus]
FROM dbo.Patient
WHERE (IsDeleted = 0) AND (IsActive = 1)
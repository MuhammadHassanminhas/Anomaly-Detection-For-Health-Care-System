SELECT
  [AppointmentID],
  [PracticeID],
  CASE
    WHEN (AppointmentCompleted IS NOT NULL) AND (ScheduleDate IS NOT NULL) THEN
      CASE
        WHEN ((AppointmentCompleted = 1) AND (ScheduleDate <= DATEADD(day, -@invoice_lag_days, sysdatetime())) AND NOT EXISTS (SELECT 1 FROM dbo.Invoices WHERE (dbo.Invoices.AppointmentID = dbo.Appointments.AppointmentID) AND (dbo.Invoices.IsActive = 1))) THEN 'fail'
        WHEN NOT ((AppointmentCompleted = 1) AND (ScheduleDate <= DATEADD(day, -@invoice_lag_days, sysdatetime())) AND NOT EXISTS (SELECT 1 FROM dbo.Invoices WHERE (dbo.Invoices.AppointmentID = dbo.Appointments.AppointmentID) AND (dbo.Invoices.IsActive = 1))) THEN 'pass'
        ELSE 'indeterminate'
      END
    ELSE 'indeterminate'
  END AS tri_state,
  [PatientID],
  [ScheduleDate],
  [AppointmentType],
  [Provider]
FROM dbo.Appointments
WHERE (IsDeleted = 0) AND (IsDummy = 0)
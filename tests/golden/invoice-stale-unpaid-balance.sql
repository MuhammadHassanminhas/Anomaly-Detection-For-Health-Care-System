SELECT
  [InvoiceTransactionID],
  [PracticeID],
  CASE
    WHEN (InvoiceDate IS NOT NULL) AND (UnpaidAmount IS NOT NULL) THEN
      CASE
        WHEN ((UnpaidAmount > 0) AND (InvoiceDate <= DATEADD(day, -@stale_days, sysdatetime()))) THEN 'fail'
        WHEN NOT ((UnpaidAmount > 0) AND (InvoiceDate <= DATEADD(day, -@stale_days, sysdatetime()))) THEN 'pass'
        ELSE 'indeterminate'
      END
    ELSE 'indeterminate'
  END AS tri_state,
  [PatientID],
  [InvoiceDate],
  [UnpaidAmount],
  [TotalAmount]
FROM fqb.Invoices
WHERE (IsDeleted = 0) AND (IsActive = 1)
SELECT
  [InvoiceTransactionID],
  [PracticeID],
  CASE
    WHEN (TotalAmount IS NOT NULL) THEN
      CASE
        WHEN (TotalAmount < 0) THEN 'fail'
        WHEN NOT (TotalAmount < 0) THEN 'pass'
        ELSE 'indeterminate'
      END
    ELSE 'indeterminate'
  END AS tri_state,
  [TotalAmount],
  [InvoiceDate]
FROM fqb.Invoices
WHERE (IsDeleted = 0)
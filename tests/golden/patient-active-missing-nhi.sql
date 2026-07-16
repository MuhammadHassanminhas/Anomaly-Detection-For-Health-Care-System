SELECT
  [ProfileID],
  [PracticeID],
  CASE
    WHEN (IsActive IS NOT NULL) THEN
      CASE
        WHEN ((IsActive = 1) AND (IsTestRecord = 0) AND (NHINumber IS NULL)) THEN 'fail'
        WHEN NOT ((IsActive = 1) AND (IsTestRecord = 0) AND (NHINumber IS NULL)) THEN 'pass'
        ELSE 'indeterminate'
      END
    ELSE 'indeterminate'
  END AS tri_state,
  [FirstName],
  [FamilyName],
  [EnrollmentDate]
FROM dbo.Patient
WHERE (IsDeleted = 0)
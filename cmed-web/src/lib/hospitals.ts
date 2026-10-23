/**
 * The hospital codes agreed with CMED: the clinic's name in capitals, words
 * joined by underscores. CMED sends one of these with every trigger and every
 * API 2 / API 3, from the doctor's login. The AIMS LAB server knows each one,
 * and each hospital's CMED login may send only its own.
 */
export const HOSPITALS = [
  'AALO_DHOLPUR',
  'AALO_ERSHADNAGAR',
  'AALO_KARAIL',
  'AALO_MIRPUR',
  'AALO_NARAYANGANJ',
  'AALO_SHYAMPUR',
  'AMADER_SUSASTHO',
] as const;

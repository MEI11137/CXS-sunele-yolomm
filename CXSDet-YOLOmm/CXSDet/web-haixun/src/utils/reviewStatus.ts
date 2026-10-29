export function reviewStatus(status: string): 'pending' | 'done' | 'rejected' {
  return status === 'rejected' ? 'rejected' : ['confirmed', 'no_defect'].includes(status) ? 'done' : 'pending'
}

export function isPendingReview(status: string): boolean {
  return reviewStatus(status) === 'pending'
}

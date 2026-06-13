export async function api(path, options = {}) {
  const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData
  const { skipAuthRedirect, ...fetchOptions } = options
  const response = await fetch(path, {
    ...fetchOptions,
    headers: {
      ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
      ...(options.headers || {})
    }
  })
  const text = await response.text()
  let data = {}
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = { raw: text }
    }
  }
  if (!response.ok) {
    const message = data.detail || data.message || response.statusText || '请求失败'
    if (response.status === 401 && !skipAuthRedirect && typeof window !== 'undefined' && window.location.pathname !== '/login') {
      const redirect = `${window.location.pathname}${window.location.search}`
      window.location.href = `/login?redirect=${encodeURIComponent(redirect)}`
    }
    throw new Error(message)
  }
  return data
}

export function postJson(path, payload, options = {}) {
  return api(path, {
    ...options,
    method: 'POST',
    body: JSON.stringify(payload || {})
  })
}

export function deleteJson(path) {
  return api(path, {
    method: 'DELETE'
  })
}

export function postFormData(path, formData) {
  return api(path, {
    method: 'POST',
    body: formData
  })
}

(function () {
  const SESSION_KEY = "lc_session";
  const API_BASE = window.location.protocol === "file:" ? "http://127.0.0.1:8000" : "";

  function getStoredSession() {
    const raw = localStorage.getItem(SESSION_KEY) || sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch (error) {
      return null;
    }
  }

  function saveSession(session, remember) {
    const store = remember ? localStorage : sessionStorage;
    const other = remember ? sessionStorage : localStorage;
    other.removeItem(SESSION_KEY);
    store.setItem(SESSION_KEY, JSON.stringify(session));
  }

  function clearSession() {
    localStorage.removeItem(SESSION_KEY);
    sessionStorage.removeItem(SESSION_KEY);
  }

  function fileToDataUrl(file) {
    return new Promise(function (resolve, reject) {
      const reader = new FileReader();
      reader.onload = function () {
        resolve({
          name: file.name,
          dataUrl: reader.result,
        });
      };
      reader.onerror = function () {
        reject(new Error("Unable to read one of the selected images."));
      };
      reader.readAsDataURL(file);
    });
  }

  async function request(path, options) {
    const session = getStoredSession();
    const headers = Object.assign(
      { "Content-Type": "application/json" },
      options && options.headers ? options.headers : {}
    );

    if (session && session.token) {
      headers.Authorization = "Bearer " + session.token;
    }

    let response;
    try {
      response = await fetch(API_BASE + path, Object.assign({}, options, { headers: headers }));
    } catch (error) {
      const networkError = new Error(
        "Cannot reach the LocalConnect server. Start `python server.py` in the project folder."
      );
      networkError.status = 0;
      throw networkError;
    }
    const isJson = (response.headers.get("content-type") || "").includes("application/json");
    const payload = isJson ? await response.json() : {};

    if (!response.ok) {
      const error = new Error(payload.error || "Request failed.");
      error.status = response.status;
      error.field = payload.field || "";
      throw error;
    }

    return payload;
  }

  window.LocalConnect = {
    getStoredSession: getStoredSession,
    saveSession: saveSession,
    clearSession: clearSession,
    async fetchSession() {
      const session = getStoredSession();
      if (!session || !session.token) return null;
      try {
        const payload = await request("/api/session", { method: "GET" });
        saveSession(payload.session, !!localStorage.getItem(SESSION_KEY));
        return payload.session;
      } catch (error) {
        clearSession();
        return null;
      }
    },
    async login(data, remember) {
      const payload = await request("/api/login", {
        method: "POST",
        body: JSON.stringify(data),
      });
      saveSession(payload.session, remember);
      return payload.session;
    },
    async register(data) {
      const payload = await request("/api/register", {
        method: "POST",
        body: JSON.stringify(data),
      });
      saveSession(payload.session, false);
      return payload.session;
    },
    async logout() {
      try {
        await request("/api/logout", { method: "POST", body: "{}" });
      } finally {
        clearSession();
      }
    },
    async getCurrentUser() {
      const payload = await request("/api/users/me", { method: "GET" });
      return payload.user;
    },
    async updateCurrentUser(data) {
      const payload = await request("/api/users/me", {
        method: "PUT",
        body: JSON.stringify(data),
      });
      const session = getStoredSession();
      if (session) {
        saveSession(
          Object.assign({}, session, {
            name: payload.user.name,
            email: payload.user.email,
            businessId: payload.user.businessId,
          }),
          !!localStorage.getItem(SESSION_KEY)
        );
      }
      return payload.user;
    },
    async changePassword(data) {
      return request("/api/users/me/password", {
        method: "PUT",
        body: JSON.stringify(data),
      });
    },
    async deleteCurrentUser() {
      return request("/api/users/me", { method: "DELETE" });
    },
    async listBusinesses() {
      const payload = await request("/api/businesses", { method: "GET" });
      return payload.businesses;
    },
    async getBusiness(id) {
      const payload = await request("/api/businesses/" + id, { method: "GET" });
      return payload.business;
    },
    async getOwnerBusiness() {
      const payload = await request("/api/owner/business", { method: "GET" });
      return payload.business;
    },
    async updateOwnerBusiness(data) {
      const payload = await request("/api/owner/business", {
        method: "PUT",
        body: JSON.stringify(data),
      });
      return payload.business;
    },
    async updateOwnerServices(services) {
      return request("/api/owner/business/services", {
        method: "PUT",
        body: JSON.stringify({ services: services }),
      });
    },
    async updateOwnerHours(hours) {
      return request("/api/owner/business/hours", {
        method: "PUT",
        body: JSON.stringify({ hours: hours }),
      });
    },
    async addOwnerBusinessImages(files) {
      const images = await Promise.all(Array.from(files || []).map(fileToDataUrl));
      return request("/api/owner/business/images", {
        method: "POST",
        body: JSON.stringify({ images: images }),
      });
    },
    async deleteOwnerBusinessImage(imageId) {
      return request("/api/owner/business/images/" + imageId, {
        method: "DELETE",
        body: "{}",
      });
    },
    async filesToListingImages(files) {
      return Promise.all(Array.from(files || []).map(fileToDataUrl));
    },
    async listReviews(businessId) {
      const payload = await request("/api/reviews?business_id=" + encodeURIComponent(businessId), {
        method: "GET",
      });
      return payload.reviews;
    },
    async submitReview(data) {
      return request("/api/reviews", {
        method: "POST",
        body: JSON.stringify(data),
      });
    },
  };
})();

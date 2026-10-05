// Modified for the XHS Post Pipeline integration; see MODIFICATIONS.md in the integration package.
package main

import (
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// setupRoutes 设置路由配置
func setupRoutes(appServer *AppServer) *gin.Engine {
	// 设置 Gin 模式
	gin.SetMode(gin.ReleaseMode)

	router := gin.New()
	router.Use(gin.Logger())
	router.Use(gin.Recovery())

	// 添加中间件
	router.Use(errorHandlingMiddleware())
	router.Use(corsMiddleware())

	// 健康检查
	router.GET("/health", healthHandler)

	// MCP 端点 - 使用官方 SDK 的 Streamable HTTP Handler
	mcpHandler := mcp.NewStreamableHTTPHandler(
		func(r *http.Request) *mcp.Server {
			return appServer.mcpServer
		},
		&mcp.StreamableHTTPOptions{
			JSONResponse: true, // 支持 JSON 响应
			// 换取客户端可跳过 initialize 握手直接调工具。代价是服务端无法反向
			// 请求客户端（sampling / elicitation / roots），眼下一处都没用到；
			// 要用得先摘掉这行。
			Stateless: true,
		},
	)
	protected := router.Group("")
	protected.Use(authMiddleware(appServer.authToken))
	protected.Use(func(c *gin.Context) {
		// A platform restriction is not resolved by trying another tool/endpoint.
		// Keep only local health and journal-aware readback available while blocked.
		path := c.Request.URL.Path
		if path == "/xhs-integration/health" || (strings.HasPrefix(path, "/xhs-integration/attempts/") && strings.HasSuffix(path, "/observations")) {
			c.Next()
			return
		}
		if blocked := currentLoginNetworkBlock(); blocked != nil {
			respondError(c, http.StatusServiceUnavailable, blocked.Code, "平台限制当前网络，后端已暂停网络操作", blocked)
			c.Abort()
			return
		}
		c.Next()
	})
	appServer.registerIntegrationRoutes(protected)

	protected.Any("/mcp", gin.WrapH(mcpHandler))
	protected.Any("/mcp/*path", gin.WrapH(mcpHandler))

	// API 路由组
	api := protected.Group("/api/v1")
	{
		api.GET("/login/status", appServer.checkLoginStatusHandler)
		api.GET("/login/qrcode", appServer.getLoginQrcodeHandler)
		api.DELETE("/login/cookies", appServer.deleteCookiesHandler)
		api.POST("/publish", appServer.publishHandler)
		api.POST("/publish_video", appServer.publishVideoHandler)
		api.GET("/feeds/list", appServer.listFeedsHandler)
		api.GET("/feeds/search", appServer.searchFeedsHandler)
		api.POST("/feeds/search", appServer.searchFeedsHandler)
		api.POST("/feeds/detail", appServer.getFeedDetailHandler)
		api.POST("/user/profile", appServer.userProfileHandler)
		api.POST("/feeds/comment", appServer.postCommentHandler)
		api.POST("/feeds/comment/reply", appServer.replyCommentHandler)
		api.POST("/feeds/like", appServer.likeFeedHandler)
		api.POST("/feeds/favorite", appServer.favoriteFeedHandler)
		api.GET("/user/me", appServer.myProfileHandler)
		api.GET("/notifications/unread", appServer.getUnreadCountHandler)
		api.GET("/notifications/list", appServer.listNotificationsHandler)
		api.POST("/notifications/list", appServer.listNotificationsHandler)
		api.POST("/notifications/reply", appServer.replyNotificationHandler)
		api.POST("/notifications/like", appServer.likeNotificationHandler)
	}

	return router
}

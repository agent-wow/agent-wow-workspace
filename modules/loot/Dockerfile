FROM golang:1.27.1 AS build
ENV GOPROXY=https://goproxy.cn,direct
WORKDIR /src
# The shared protocol library is a sibling module wired in through a replace
# directive, so the root build context must copy it before go mod download.
COPY lib/wow ./lib/wow
COPY modules/loot/go.mod modules/loot/go.sum ./modules/loot/
WORKDIR /src/modules/loot
RUN go mod download
COPY modules/loot ./
RUN CGO_ENABLED=0 go build -trimpath -o /module ./cmd

FROM scratch
COPY --from=build /module /module
ENTRYPOINT ["/module"]

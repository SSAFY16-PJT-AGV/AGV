
#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "AGVPathActor.generated.h"

class USplineComponent;
class USplineMeshComponent;
class UStaticMesh;

UCLASS()
class AGVSIMULATION_API AAGVPathActor : public AActor
{
    GENERATED_BODY()

public:
    AAGVPathActor();

    // 시작점, 중간 경유점, 끝점을 받아 경로 생성
    void BuildPath(const TArray<FVector>& PathPoints);

protected:
    UPROPERTY(VisibleAnywhere, Category = "AGV Path")
    TObjectPtr<USplineComponent> PathSpline;

    // 경로 표시용 메시
    UPROPERTY(EditDefaultsOnly, Category = "AGV Path")
    TObjectPtr<UStaticMesh> PathMesh;

    UPROPERTY(EditDefaultsOnly, Category = "AGV Path")
    float PathWidth = 12.0f;

    UPROPERTY(EditDefaultsOnly, Category = "AGV Path")
    float PathHeight = 1.0f;

    UPROPERTY()
    TArray<TObjectPtr<USplineMeshComponent>> PathSegments;
};
